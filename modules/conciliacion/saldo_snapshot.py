"""Snapshots del "Saldo a conciliar" por evento de conciliación.

TMT 2026-05-28 dueña: "cuando yo hago una conciliacion que se actualicce,
si no no. y que muestre los anteriores".

Modelo:
  - Cada vez que se crea/deshace un match o se marca/desmarca una histórica,
    insertamos un snapshot con el saldo_a_conciliar resultante.
  - El UI muestra siempre el ÚLTIMO snapshot como "Saldo a conciliar".
  - Movs nuevos en transacciones_bancarias NO disparan snapshot — el número
    se mantiene estable hasta la próxima conciliación.

Fórmula del saldo_a_conciliar (igual que la vista actual):
  saldo_pc_libros − pendientes_signed
donde pendientes_signed cuenta movs PC no conciliados con docto C/D.
"""

from __future__ import annotations

import logging

import db as _db

_LOG = logging.getLogger("programa_core.conciliacion.snapshot")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.banco_saldo_conc_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    no_banco        INTEGER NOT NULL,
    saldo_pc        NUMERIC(14, 2) NOT NULL,
    pend_signed     NUMERIC(14, 2) NOT NULL,
    saldo_conc      NUMERIC(14, 2) NOT NULL,
    n_pendientes    INTEGER NOT NULL DEFAULT 0,
    evento_tipo     TEXT NOT NULL,
    evento_ref      TEXT,
    usuario         TEXT,
    descripcion     TEXT,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bsc_banco_ts
    ON scintela.banco_saldo_conc_snapshot (no_banco, creado_en DESC);
"""
_bootstrapped = False


def _bootstrap() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL)
    except Exception as exc:
        _LOG.exception("bootstrap snapshot table failed: %s", exc)
    _bootstrapped = True


def snapshot(
    no_banco: int,
    evento_tipo: str,
    *,
    evento_ref: str | int | None = None,
    usuario: str | None = None,
    descripcion: str | None = None,
) -> int | None:
    """Calcula saldo_a_conciliar actual y guarda un snapshot.

    Devuelve el id del snapshot, o None si algo falla (fail-soft).
    """
    _bootstrap()
    try:
        # Saldo PC libros
        row_pc = _db.fetch_one(
            """
            SELECT t.saldo
              FROM scintela.transacciones_bancarias t
             WHERE t.no_banco = %s AND t.saldo IS NOT NULL
             ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1
            """,
            (int(no_banco),),
        )
        saldo_pc = float((row_pc or {}).get("saldo") or 0)

        # Pendientes (movs PC sin conciliar)
        row_pend = _db.fetch_one(
            """
            SELECT COUNT(*) AS n,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN -t.importe ELSE t.importe END), 0) AS signed
            FROM scintela.transacciones_bancarias t
            WHERE t.no_banco = %s
              AND TRIM(COALESCE(t.stat,'')) <> '*'
              AND NOT EXISTS (
                  SELECT 1 FROM scintela.banco_conciliacion_match m
                   WHERE m.id_transaccion = t.id_transaccion AND m.deshecho_en IS NULL
              )
            """,
            (int(no_banco),),
        ) or {}
        pend_signed = float(row_pend.get("signed") or 0)
        n_pend = int(row_pend.get("n") or 0)
        saldo_conc = round(saldo_pc - pend_signed, 2)

        # Insert
        row = _db.fetch_one(
            """
            INSERT INTO scintela.banco_saldo_conc_snapshot
                (no_banco, saldo_pc, pend_signed, saldo_conc, n_pendientes,
                 evento_tipo, evento_ref, usuario, descripcion)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (int(no_banco), saldo_pc, pend_signed, saldo_conc, n_pend,
             evento_tipo[:40],
             str(evento_ref)[:80] if evento_ref is not None else None,
             (usuario or "web")[:50],
             (descripcion or "")[:200] or None),
        )
        return int(row["id"]) if row else None
    except Exception as exc:
        _LOG.exception("snapshot save failed: %s", exc)
        return None


def ultimo(no_banco: int) -> dict | None:
    """Devuelve el último snapshot para el banco (o None)."""
    _bootstrap()
    try:
        return _db.fetch_one(
            """
            SELECT id, saldo_pc, pend_signed, saldo_conc, n_pendientes,
                   evento_tipo, evento_ref, usuario, descripcion, creado_en
              FROM scintela.banco_saldo_conc_snapshot
             WHERE no_banco = %s
             ORDER BY creado_en DESC, id DESC LIMIT 1
            """,
            (int(no_banco),),
        )
    except Exception:
        return None


def historial(no_banco: int, limit: int = 50) -> list[dict]:
    """Lista los snapshots ordenados por timestamp desc."""
    _bootstrap()
    try:
        return _db.fetch_all(
            """
            SELECT id, saldo_pc, pend_signed, saldo_conc, n_pendientes,
                   evento_tipo, evento_ref, usuario, descripcion, creado_en
              FROM scintela.banco_saldo_conc_snapshot
             WHERE no_banco = %s
             ORDER BY creado_en DESC, id DESC LIMIT %s
            """,
            (int(no_banco), int(limit)),
        ) or []
    except Exception:
        return []
