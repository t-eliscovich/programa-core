"""Helpers para escribir movimientos en `scintela.caja` con saldo running.

Mismo patrón que `bank_helpers.py` pero contra la tabla `caja`. La caja es
mono-cuenta (no hay no_banco / no_cta), así que es más simple.

Convenciones de signo (heredadas de BANCOS.PRG.PASOCAJA):
    SIGNO = +1 si tipo == 'E'  (entrada)
    SIGNO = -1 si tipo == 'S'  (salida)

`importe` siempre positivo. El signo vive en `tipo`.
"""
from __future__ import annotations

from datetime import date

import db


def signo_tipo(tipo: str) -> int:
    """+1 entrada, -1 salida."""
    return 1 if (tipo or "").upper().strip() == "E" else -1


def saldo_actual(conn=None) -> float:
    """Saldo más reciente de caja. 0.0 si vacía."""
    row = db.fetch_one(
        """
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         ORDER BY fecha DESC NULLS LAST, id_caja DESC
         LIMIT 1
        """,
        conn=conn,
    )
    return float(row["saldo"]) if row else 0.0


def _saldo_previo(
    conn, *, fecha: date, excluir_id: int | None = None,
    solo_dias_anteriores: bool = False,
) -> float:
    """Saldo anterior al movimiento que se está por insertar.

    `solo_dias_anteriores=True` ancla ESTRICTO en `fecha < ancla` (cierre del
    día anterior) — lo necesita recompute_saldos_desde(ancla_fecha=...) porque
    el walk re-aplica todas las filas de la fecha ancla. Mismo bug/fix que
    bank_helpers TMT 2026-06-11 (backdated recompute).
    """
    if solo_dias_anteriores:
        cond_fecha = "(fecha < %s)"
        params_fecha: tuple = (fecha,)
    else:
        cond_fecha = (
            "((fecha < %s) OR (fecha = %s AND (%s::int IS NULL "
            "OR id_caja < %s::int)))"
        )
        params_fecha = (fecha, fecha, excluir_id, excluir_id)
    row = db.fetch_one(
        f"""
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         WHERE {cond_fecha}
         ORDER BY fecha DESC, id_caja DESC
         LIMIT 1
        """,
        params_fecha,
        conn=conn,
    )
    return float(row["saldo"]) if row else 0.0


def insert_movimiento_caja(
    conn,
    *,
    fecha: date,
    tipo: str,
    importe: float,
    concepto: str,
    clave: str | None = None,
    id_cheque: int | None = None,
    usuario: str = "web",
) -> dict:
    """Inserta un movimiento de caja con saldo running calculado.

    Devuelve `{id_caja, saldo_nuevo, saldo_anterior, signo}`.

    `tipo` debe ser 'E' (entrada) o 'S' (salida). Importe siempre positivo.
    El signo se aplica internamente.
    """
    tipo_norm = (tipo or "").upper().strip()
    if tipo_norm not in ("E", "S"):
        raise ValueError(f"tipo debe ser 'E' o 'S' (recibido: {tipo!r})")
    importe_f = float(importe or 0)
    if importe_f <= 0:
        raise ValueError(f"importe debe ser > 0 (recibido: {importe!r})")
    importe_abs = importe_f

    signo = signo_tipo(tipo_norm)
    saldo_anterior = _saldo_previo(conn, fecha=fecha)
    saldo_nuevo = round(saldo_anterior + signo * importe_abs, 2)

    row = db.execute_returning(
        """
        INSERT INTO scintela.caja
            (fecha, tipo, importe, concepto, saldo, clave,
             id_cheque, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_caja
        """,
        (
            fecha,
            tipo_norm,
            importe_abs,
            (concepto or "").strip()[:100],
            saldo_nuevo,
            (clave or None) and clave[:3].upper(),
            id_cheque,
            usuario[:50],
        ),
        conn=conn,
    ) or {}

    return {
        "id_caja": row.get("id_caja"),
        "saldo_nuevo": saldo_nuevo,
        "saldo_anterior": saldo_anterior,
        "signo": signo,
        "importe": importe_abs,
    }


def recompute_saldos_desde(
    conn,
    *,
    ancla_id: int | None = None,
    ancla_fecha: date | None = None,
) -> int:
    """Walk-forward: recalcula `saldo` para toda fila >= ancla.

    Mismo contrato que bank_helpers.recompute_saldos_desde pero contra caja.
    Sólo necesario para correcciones administrativas. Devuelve cantidad de
    filas actualizadas.
    """
    # Blindaje TMT 2026-05-13: NUNCA aceptar ambos None sin un flag explícito.
    # Pasar sin ancla destruye el saldo opening histórico — mismo bug que
    # tuvo bank_helpers (#79, #80).
    if ancla_id is None and ancla_fecha is None:
        raise ValueError(
            "recompute_saldos_desde de caja requiere ancla_id o ancla_fecha. "
            "Sin ancla, se rebobina desde 0 y se pierde el opening histórico."
        )
    if ancla_id is not None:
        row = db.fetch_one(
            """
            SELECT COALESCE(saldo, 0) AS saldo
              FROM scintela.caja
             WHERE id_caja < %s
             ORDER BY id_caja DESC
             LIMIT 1
            """,
            (ancla_id,),
            conn=conn,
        )
        saldo = float(row["saldo"]) if row else 0.0
        cond_inicio = "id_caja >= %s"
        params_inicio: tuple = (ancla_id,)
    else:  # ancla_fecha
        # TMT 2026-06-11: ancla = cierre del día ANTERIOR (estricto), el walk
        # re-aplica todas las filas de la fecha ancla. Ver bank_helpers.
        saldo = _saldo_previo(conn, fecha=ancla_fecha, solo_dias_anteriores=True)
        cond_inicio = "fecha >= %s::date"
        params_inicio = (ancla_fecha,)

    rows = db.fetch_all(
        f"""
        SELECT id_caja, tipo, importe
          FROM scintela.caja
         WHERE {cond_inicio}
         ORDER BY fecha, id_caja
        """,
        params_inicio,
        conn=conn,
    ) or []

    n = 0
    for r in rows:
        signo = signo_tipo(r["tipo"])
        saldo = round(saldo + signo * abs(float(r["importe"] or 0)), 2)
        db.execute(
            "UPDATE scintela.caja "
            "SET saldo = %s, fecha_modifica = CURRENT_TIMESTAMP "
            "WHERE id_caja = %s",
            (saldo, r["id_caja"]),
            conn=conn,
        )
        n += 1
    return n
