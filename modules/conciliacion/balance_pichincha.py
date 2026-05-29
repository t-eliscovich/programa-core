"""Helper: calcular el dict saldo_pc_actual para el banco Pichincha.

Extraído de views.hub() (líneas ~752-930) para poder reusarlo desde la
pantalla post-procesar nueva (banco_v2). Lógica idéntica — solo se
encapsula. Si esto se rompe, la pantalla original tira el mismo error.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("programa_core.conciliacion.balance_pichincha")

_BANCO_PICHINCHA = 10


def calcular(no_banco: int = _BANCO_PICHINCHA) -> dict:
    """Devuelve el dict con todos los componentes del balance live."""
    import db as _db

    saldo_pc_actual: dict = {}
    try:
        row_actual = _db.fetch_one(
            """
            SELECT t.fecha, t.saldo, t.id_transaccion
              FROM scintela.transacciones_bancarias t
             WHERE t.no_banco = %(no_banco)s
               AND t.saldo IS NOT NULL
             ORDER BY t.fecha DESC, t.id_transaccion DESC
             LIMIT 1
            """,
            {"no_banco": no_banco},
        ) or {}
        saldo_pc_actual = {
            "saldo": float(row_actual.get("saldo") or 0),
            "fecha": row_actual.get("fecha"),
            "id_transaccion": row_actual.get("id_transaccion"),
        }

        try:
            from modules.conciliacion import saldo_snapshot as _ss
            ult_snap = _ss.ultimo(no_banco)
            if ult_snap and ult_snap.get("saldo_conc") is not None:
                saldo_pc_actual["saldo_a_conciliar_estable"] = float(ult_snap["saldo_conc"])
                saldo_pc_actual["snapshot_evento"] = ult_snap.get("evento_tipo")
                saldo_pc_actual["snapshot_fecha"] = ult_snap.get("creado_en")
            else:
                saldo_pc_actual["saldo_a_conciliar_estable"] = None
        except Exception:
            saldo_pc_actual["saldo_a_conciliar_estable"] = None

        # Pendientes históricos banco
        row_pend = _db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN tipo = 'C' THEN monto ELSE -monto END), 0) AS neto_pend,
              COALESCE(SUM(CASE WHEN tipo = 'C' THEN monto ELSE 0 END), 0) AS sum_cred,
              COALESCE(SUM(CASE WHEN tipo = 'D' THEN monto ELSE 0 END), 0) AS sum_deb,
              COALESCE(SUM(CASE WHEN tipo = 'C' THEN 1 ELSE 0 END), 0) AS n_cred,
              COALESCE(SUM(CASE WHEN tipo = 'D' THEN 1 ELSE 0 END), 0) AS n_deb,
              COUNT(*) AS n_pend
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %(no_banco)s
               AND conciliado_en IS NULL
            """,
            {"no_banco": no_banco},
        ) or {}
        saldo_pc_actual["neto_pendientes"] = float(row_pend.get("neto_pend") or 0)
        saldo_pc_actual["n_pendientes"] = int(row_pend.get("n_pend") or 0)
        saldo_pc_actual["pendientes_banco_creditos"] = round(float(row_pend.get("sum_cred") or 0), 2)
        saldo_pc_actual["pendientes_banco_debitos"] = round(float(row_pend.get("sum_deb") or 0), 2)
        saldo_pc_actual["n_pendientes_banco_cred"] = int(row_pend.get("n_cred") or 0)
        saldo_pc_actual["n_pendientes_banco_deb"] = int(row_pend.get("n_deb") or 0)

        # Pendientes PC
        row_pend_hoy = _db.fetch_one(
            """
            SELECT
              COUNT(*) AS n,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN -t.importe ELSE t.importe END), 0) AS signed,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN 0 ELSE t.importe END), 0) AS sum_cred,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN t.importe ELSE 0 END), 0) AS sum_deb,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN 0 ELSE 1 END), 0) AS n_cred,
              COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN 1 ELSE 0 END), 0) AS n_deb
              FROM scintela.transacciones_bancarias t
             WHERE t.no_banco = %(no_banco)s
               AND TRIM(COALESCE(t.stat, '')) <> '*'
               AND NOT EXISTS (
                   SELECT 1 FROM scintela.banco_conciliacion_match m
                    WHERE m.id_transaccion = t.id_transaccion
                      AND m.deshecho_en IS NULL
               )
            """,
            {"no_banco": no_banco},
        ) or {}
        saldo_pc_actual["n_pendientes_conciliar"] = int(row_pend_hoy.get("n") or 0)
        saldo_pc_actual["pendientes_conciliar_neto"] = round(float(row_pend_hoy.get("signed") or 0), 2)
        saldo_pc_actual["pendientes_pc_creditos"] = round(float(row_pend_hoy.get("sum_cred") or 0), 2)
        saldo_pc_actual["pendientes_pc_debitos"] = round(float(row_pend_hoy.get("sum_deb") or 0), 2)
        saldo_pc_actual["n_pendientes_pc_cred"] = int(row_pend_hoy.get("n_cred") or 0)
        saldo_pc_actual["n_pendientes_pc_deb"] = int(row_pend_hoy.get("n_deb") or 0)
        saldo_pc_actual["saldo_si_concilio_todo"] = round(
            saldo_pc_actual["saldo"] - saldo_pc_actual["pendientes_conciliar_neto"], 2
        )
        saldo_pc_actual["saldo_banco_esperado"] = round(
            saldo_pc_actual["saldo_si_concilio_todo"] + saldo_pc_actual["neto_pendientes"], 2
        )
        try:
            saldo_pc_actual["pendientes_conciliar_rows"] = _db.fetch_all(
                """
                SELECT t.id_transaccion, t.fecha, t.documento, t.no_cheque,
                       t.concepto, t.importe,
                       CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                            THEN -t.importe ELSE t.importe END AS importe_signed
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %(no_banco)s
                   AND TRIM(COALESCE(t.stat, '')) <> '*'
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = t.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 30
                """,
                {"no_banco": no_banco},
            ) or []
        except Exception:
            saldo_pc_actual["pendientes_conciliar_rows"] = []
    except Exception as e:
        _LOG.exception("calcular() falló: %s", e)
        return {}
    return saldo_pc_actual
