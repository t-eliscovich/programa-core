"""Papelera de pendientes del banco — la ✕ deja de ser irreversible.

⭐ POR QUÉ (TMT 2026-08-03). Borrar un pendiente HISTÓRICO
(`banco_v2_view.banco_eliminar_pendiente`) es un `DELETE` de verdad: la fila
se va y no vuelve. "↩ Restaurar borradas" recupera SÓLO filas del extracto de
la sesión, no históricos.

Y borrar uno **baja el "Saldo banco esperado" por su importe exacto**, o sea
mueve la diferencia de la conciliación. Es la única acción de la app que toca
plata y no se puede deshacer.

El rastro que había era insuficiente: `banco_saldo_conc_snapshot` guarda el
**id** del pendiente y el saldo que quedó, pero **ni el importe ni el
concepto** — y la fila ya no existe para ir a buscarlos. El día que la dueña
borró 6 filas de basura ($8.304.132,19 de resumen contable cargado como
pendientes) no hubo forma de reconstruir qué se había sacado, y hubo que
deducirlo de los Excel que ella tenía en la compu.

## Por qué una papelera y no un soft-delete

`banco_historicos_pendientes` se LEE desde **18 SELECT en 8 archivos** (el
balance de Pichincha, la sesión, el matcher, el hub, el health, el generador
del xlsx…). Marcar la fila como borrada en vez de eliminarla obliga a filtrar
en las 18: si se escapa UNA, la fila vuelve a la suma de pendientes y la
diferencia de la conciliación se mueve por ese importe.

Una tabla aparte tiene **cero superficie**: ninguna query de pendientes
cambia. El `DELETE` sigue igual; lo único nuevo es que antes se copia la fila.
Mismo patrón que `/bancos/papelera` para transacciones bancarias.

El bootstrap es `CREATE TABLE IF NOT EXISTS` en caliente porque **el deploy no
corre migraciones** — igual que `saldo_snapshot`.
"""
from __future__ import annotations

import logging

import db as _db

_LOG = logging.getLogger("programa_core.conciliacion.papelera")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.banco_pendientes_papelera (
    id              BIGSERIAL PRIMARY KEY,
    hist_id         BIGINT,                 -- id que tenía en el backlog
    no_banco        INTEGER NOT NULL,
    fecha           DATE,
    concepto        TEXT,
    documento       TEXT,
    monto           NUMERIC(14,2) NOT NULL,
    tipo            TEXT,
    oficina         TEXT,
    detalle         TEXT,
    codigo          TEXT,
    fuente          TEXT,
    creado_en_orig  TIMESTAMP,              -- cuándo había entrado al backlog
    sesion_id       INTEGER,                -- desde qué sesión se borró
    borrado_por     TEXT,
    borrado_en      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    restaurado_en   TIMESTAMP,
    restaurado_por  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bpp_banco_ts
    ON scintela.banco_pendientes_papelera (no_banco, borrado_en DESC);
"""
_bootstrapped = False


def _bootstrap() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("bootstrap papelera falló: %s", exc)
    _bootstrapped = True


# La papelera NUNCA puede romper el borrado: si algo falla acá, la ✕ tiene que
# seguir funcionando. Todo va envuelto y fail-soft.
def guardar(fila: dict, *, no_banco: int, usuario: str = "web",
            sesion_id: int | None = None, conn=None) -> int | None:
    """Copia la fila del backlog a la papelera ANTES del DELETE.

    Devuelve el id en la papelera, o None si no se pudo guardar (y en ese caso
    el borrado sigue adelante igual — se pierde la traza, no la acción).
    """
    if not fila:
        return None
    _bootstrap()
    try:
        row = _db.execute_returning(
            """
            INSERT INTO scintela.banco_pendientes_papelera
                (hist_id, no_banco, fecha, concepto, documento, monto, tipo,
                 oficina, detalle, codigo, fuente, creado_en_orig,
                 sesion_id, borrado_por)
            VALUES (%(hist_id)s, %(no_banco)s, %(fecha)s, %(concepto)s,
                    %(documento)s, %(monto)s, %(tipo)s, %(oficina)s,
                    %(detalle)s, %(codigo)s, %(fuente)s, %(creado_en)s,
                    %(sesion_id)s, %(usuario)s)
            RETURNING id
            """,
            {
                "hist_id": fila.get("id"),
                "no_banco": no_banco,
                "fecha": fila.get("fecha"),
                "concepto": (fila.get("concepto") or "")[:200],
                "documento": (fila.get("documento") or "")[:50],
                "monto": fila.get("monto") or 0,
                "tipo": (fila.get("tipo") or "C")[:1],
                "oficina": (fila.get("oficina") or "")[:40],
                "detalle": (fila.get("detalle") or "")[:100],
                "codigo": (fila.get("codigo") or "")[:40],
                "fuente": (fila.get("fuente") or "")[:100],
                "creado_en": fila.get("creado_en"),
                "sesion_id": sesion_id or None,
                "usuario": (usuario or "web")[:50],
            },
            conn=conn,
        )
        return int(row["id"]) if row else None
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("no pude guardar el pendiente en la papelera: %s", exc)
        return None


def listar(no_banco: int, *, limite: int = 200, solo_vivos: bool = False) -> list[dict]:
    """Lo que se borró, lo más reciente primero."""
    _bootstrap()
    try:
        return _db.fetch_all(
            f"""
            SELECT id, hist_id, fecha, concepto, documento, monto, tipo,
                   oficina, fuente, sesion_id, borrado_por, borrado_en,
                   restaurado_en, restaurado_por
              FROM scintela.banco_pendientes_papelera
             WHERE no_banco = %s
               {"AND restaurado_en IS NULL" if solo_vivos else ""}
             ORDER BY borrado_en DESC
             LIMIT %s
            """,
            (no_banco, int(limite)),
        ) or []
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("no pude listar la papelera: %s", exc)
        return []


def restaurar(id_papelera: int, *, no_banco: int, usuario: str = "web") -> dict:
    """Devuelve el pendiente al backlog.

    Devuelve `{ok, motivo, concepto, monto}`. No explota nunca: la pantalla
    muestra el motivo.
    """
    _bootstrap()
    fila = _db.fetch_one(
        """
        SELECT * FROM scintela.banco_pendientes_papelera
         WHERE id = %s AND no_banco = %s
        """,
        (int(id_papelera), no_banco),
    )
    if not fila:
        return {"ok": False, "motivo": "No encontré esa fila en la papelera."}
    if fila.get("restaurado_en"):
        return {"ok": False, "motivo": "Ese pendiente ya se había restaurado.",
                "concepto": fila.get("concepto"), "monto": fila.get("monto")}

    with _db.tx() as conn:
        # `ux_bhp_firma` (banco+fecha+documento+monto) hace que restaurar dos
        # veces, o restaurar algo que volvió a entrar por el xlsx, no duplique.
        ins = _db.execute(
            """
            INSERT INTO scintela.banco_historicos_pendientes
                (no_banco, fecha, concepto, documento, monto, tipo,
                 oficina, detalle, codigo, fuente, creado_en, creado_por)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CURRENT_TIMESTAMP, %s)
            ON CONFLICT DO NOTHING
            """,
            (no_banco, fila.get("fecha"), fila.get("concepto"),
             fila.get("documento"), fila.get("monto"), fila.get("tipo") or "C",
             fila.get("oficina"), fila.get("detalle"), fila.get("codigo"),
             f"papelera:{id_papelera}", (usuario or "web")[:50]),
            conn=conn,
        ) or 0
        _db.execute(
            """
            UPDATE scintela.banco_pendientes_papelera
               SET restaurado_en = CURRENT_TIMESTAMP, restaurado_por = %s
             WHERE id = %s
            """,
            ((usuario or "web")[:50], int(id_papelera)), conn=conn,
        )

    if not ins:
        return {
            "ok": False,
            "motivo": ("Ese pendiente ya estaba en el backlog (misma fecha, "
                       "documento e importe). Lo marqué como restaurado igual."),
            "concepto": fila.get("concepto"), "monto": fila.get("monto"),
        }
    _LOG.warning("RESTAURAR pendiente de papelera #%s por %s: %s %s %.2f",
                 id_papelera, usuario, fila.get("concepto"),
                 fila.get("documento") or "—", float(fila.get("monto") or 0))
    return {"ok": True, "motivo": "", "concepto": fila.get("concepto"),
            "monto": fila.get("monto")}
