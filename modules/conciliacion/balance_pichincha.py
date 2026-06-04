"""Helper: calcular el dict saldo_pc_actual para el banco Pichincha.

Extraído de views.hub() (líneas ~752-930) para poder reusarlo desde la
pantalla post-procesar nueva (banco_v2). Lógica idéntica — solo se
encapsula. Si esto se rompe, la pantalla original tira el mismo error.
"""
from __future__ import annotations

import json as _json
import logging
from datetime import date as _date
from datetime import timedelta as _td

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

        # ── Separar PENDIENTES REALES vs CARGOS DEL BANCO ──────────────
        # TMT 2026-06-04 dueña: los cargos del banco (comisiones, IVA, costos
        # de cheque, neto del par SENAE/acreditación de aduana) NO son
        # pendientes reales — no cruzan nunca, hay que asentarlos. Van a una
        # línea "Cargos del banco (a asentar)" = la diferencia, fuera de
        # pendientes. Si la clasificación no encuentra cargos, todo queda como
        # antes (fallback seguro). Ver modules/conciliacion/cargos_banco.py.
        saldo_pc_actual["cargos_creditos"] = 0.0
        saldo_pc_actual["cargos_debitos"] = 0.0
        saldo_pc_actual["cargos_neto"] = 0.0
        saldo_pc_actual["n_cargos"] = 0
        try:
            _hrows = _db.fetch_all(
                """
                SELECT documento, concepto, monto, tipo
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %(no_banco)s AND conciliado_en IS NULL
                """,
                {"no_banco": no_banco},
            ) or []
            from modules.conciliacion import cargos_banco as _cb
            _items = [
                {"documento": r.get("documento"), "concepto": r.get("concepto"),
                 "monto": float(r.get("monto") or 0) * (1 if (r.get("tipo") or "C").strip().upper().startswith("C") else -1)}
                for r in _hrows
            ]
            _res = _cb.resumen(_items)
            if _res["cargos"]["n"] > 0:
                _r = _res["reales"]; _c = _res["cargos"]
                saldo_pc_actual["pendientes_banco_creditos"] = _r["creditos"]
                saldo_pc_actual["pendientes_banco_debitos"] = _r["debitos"]
                saldo_pc_actual["neto_pendientes"] = _r["neto"]
                saldo_pc_actual["n_pendientes"] = _r["n"]
                saldo_pc_actual["cargos_creditos"] = _c["creditos"]
                saldo_pc_actual["cargos_debitos"] = _c["debitos"]
                saldo_pc_actual["cargos_neto"] = _c["neto"]
                saldo_pc_actual["n_cargos"] = _c["n"]
        except Exception as _e:
            _LOG.warning("clasificar cargos banco falló: %s", _e)

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

        # --- TMT 2026-06-04: extracto crudo YA NO se suma a pend_banco ---
        # Pendientes de banco = la hoja (banco_historicos_pendientes). El
        # extracto de la sesión es solo insumo para cruzar. Ver nota larga en
        # la sección TOTAL más abajo. Este bloque sigue calculando sess_* por
        # ahora (cleanup de Sprint 2), pero la sección TOTAL los ignora.
        sess_neto = 0.0
        sess_cred = 0.0
        sess_deb = 0.0
        sess_n = 0
        try:
            sess_row = _db.fetch_one(
                """
                SELECT id, extracto_payload
                  FROM scintela.banco_conciliacion_sesion
                 WHERE no_banco = %(no_banco)s AND cerrada_en IS NULL
                 ORDER BY abierta_en DESC LIMIT 1
                """,
                {"no_banco": no_banco},
            )
            if sess_row and sess_row.get("extracto_payload"):
                payload = sess_row["extracto_payload"]
                if isinstance(payload, str):
                    try:
                        payload = _json.loads(payload)
                    except Exception:
                        payload = []
                movs = payload if isinstance(payload, list) else (payload.get("extracto") or payload.get("movs") or [])
                # Movs ya conciliados (firma en matches activos)
                match_firmas = set()
                try:
                    mr = _db.fetch_all(
                        """
                        SELECT real_fecha, real_documento, real_monto, real_tipo
                          FROM scintela.banco_conciliacion_match
                         WHERE no_banco = %(no_banco)s
                           AND deshecho_en IS NULL
                           AND real_documento IS NOT NULL
                        """,
                        {"no_banco": no_banco},
                    ) or []
                    for r in mr:
                        match_firmas.add((
                            str(r.get("real_fecha")),
                            (r.get("real_documento") or "").strip(),
                            round(float(r.get("real_monto") or 0), 2),
                            (r.get("real_tipo") or "").strip(),
                        ))
                except Exception:
                    pass
                # TMT 2026-06-03 dueña: 'no se deberian duplicar los extractos
                # del banco si estamos comparandolos para que no haya duplicados'.
                # Filas del payload que ya existen como histo pendiente NO se
                # cuentan acá — ya están contadas en `n_pendientes` (los histos).
                # Sin este dedup, una sesión que re-incluye un histo viejo lo
                # contaría dos veces (una en histos, otra en extracto).
                histo_firmas: set = set()
                try:
                    hr = _db.fetch_all(
                        """
                        SELECT fecha, documento, monto, tipo
                          FROM scintela.banco_historicos_pendientes
                         WHERE no_banco = %(no_banco)s
                           AND conciliado_en IS NULL
                           AND documento IS NOT NULL AND documento <> ''
                        """,
                        {"no_banco": no_banco},
                    ) or []
                    for r in hr:
                        histo_firmas.add((
                            str(r.get("fecha")),
                            (r.get("documento") or "").strip(),
                            round(float(r.get("monto") or 0), 2),
                            (r.get("tipo") or "").strip().upper()[:1],
                        ))
                except Exception:
                    pass

                # ─── DEDUP NUCLEAR vs scintela.transacciones_bancarias ───────
                # TMT decisión 2026-06-03 (dueña, literal): "no podemos tener
                # movimientos dobles. No dupliques hace un mecanismo para no
                # duplicar." Antes el dedup solo miraba matches activos +
                # histos pendientes — si la dueña re-sincaba PC los matches
                # se borraban y el extracto crudo entraba doble (una vez en
                # transacciones_bancarias, otra como "pendiente_extracto").
                #
                # Ahora: para CADA fila del extracto a contar como pendiente,
                # buscamos si ya existe una tx en transacciones_bancarias con
                # firma "razonable" (fecha ±1 día, abs(importe) ±$0.01, tipo
                # C/D consistente). Si existe → NO se cuenta como pendiente.
                #
                # Clave de tx_firmas: (fecha_iso, abs(importe_redondeado_2), tipo)
                # con expansion ±1 día. NO usamos documento porque el documento
                # del extracto (ej. "38078012") y el de PC (ej. "CH", "DE") usan
                # nomenclaturas distintas — son incomparables.
                tx_firmas: set = set()
                try:
                    fechas_movs: list[str] = []
                    for m in movs:
                        fv = m.get("fecha")
                        if not fv:
                            continue
                        s = str(fv)[:10]
                        if len(s) == 10 and s.count("-") == 2:
                            fechas_movs.append(s)
                    if fechas_movs:
                        fechas_movs.sort()
                        f_min, f_max = fechas_movs[0], fechas_movs[-1]
                        tx_rows = _db.fetch_all(
                            """
                            SELECT t.fecha, t.documento, t.importe
                              FROM scintela.transacciones_bancarias t
                             WHERE t.no_banco = %(no_banco)s
                               AND t.importe IS NOT NULL
                               AND t.fecha BETWEEN (%(f_min)s::date - INTERVAL '2 days')
                                              AND (%(f_max)s::date + INTERVAL '2 days')
                            """,
                            {"no_banco": no_banco, "f_min": f_min, "f_max": f_max},
                        ) or []
                        for r in tx_rows:
                            t_fecha = r.get("fecha")
                            t_doc = (r.get("documento") or "").strip().upper()
                            try:
                                t_imp = round(abs(float(r.get("importe") or 0)), 2)
                            except (TypeError, ValueError):
                                continue
                            if t_imp == 0:
                                continue
                            t_tipo = "D" if t_doc in ("CH", "ND", "DB", "GS", "PA") else "C"
                            # Expansion ±1 día — banco y PC a veces bookean
                            # con desfase de 1 día.
                            if isinstance(t_fecha, _date):
                                for d in (-1, 0, 1):
                                    fk = (t_fecha + _td(days=d)).isoformat()
                                    tx_firmas.add((fk, t_imp, t_tipo))
                            else:
                                # fallback: si por algún motivo no es date
                                tx_firmas.add((str(t_fecha)[:10], t_imp, t_tipo))
                except Exception:
                    _LOG.exception("calcular(): error construyendo tx_firmas (dedup nuclear)")

                # Counter de cuántos extractos quedaron deduped vs tx — útil para debug
                dedup_vs_tx = 0
                for m in movs:
                    fecha = m.get("fecha")
                    doc = (m.get("documento") or m.get("doc") or "").strip()
                    monto = round(float(m.get("monto") or m.get("importe") or 0), 2)
                    tipo = (m.get("tipo") or m.get("clase") or "").strip().upper()[:1] or ("C" if monto > 0 else "D")
                    key = (str(fecha), doc, abs(monto), tipo)
                    if key in match_firmas:
                        continue
                    # Dedup vs histos pendientes — ya contados en n_pendientes.
                    if (str(fecha), doc, abs(monto), tipo) in histo_firmas:
                        continue
                    # Dedup NUCLEAR vs transacciones_bancarias.
                    if (str(fecha)[:10], abs(monto), tipo) in tx_firmas:
                        dedup_vs_tx += 1
                        continue
                    sess_n += 1
                    amt = abs(monto)
                    if tipo == "C":
                        sess_cred += amt
                        sess_neto += amt
                    else:
                        sess_deb += amt
                        sess_neto -= amt
                saldo_pc_actual["dedup_vs_tx"] = dedup_vs_tx
        except Exception as _e:
            _LOG.exception("calcular(): error sumando extracto sesion: %s", _e)

        saldo_pc_actual["pendientes_banco_extracto_creditos"] = round(sess_cred, 2)
        saldo_pc_actual["pendientes_banco_extracto_debitos"] = round(sess_deb, 2)
        saldo_pc_actual["n_pendientes_banco_extracto"] = sess_n
        saldo_pc_actual["neto_pendientes_extracto"] = round(sess_neto, 2)

        # ── TOTAL pend_banco = la HOJA (históricos), NO el extracto crudo ──
        # TMT 2026-06-04 dueña: "lo único que se tiene que mantener como
        # pendientes es lo del archivo (la hoja)". El extracto de la sesión
        # es solo el insumo para CRUZAR/parear; no define pendientes de banco.
        # El viejo 'FIX 2026-06-03' lo sumaba acá y metía ~−557k fantasma en
        # la pantalla activa: el extracto trae el día entero del banco (SENAE,
        # débitos ya en libros) y el dedup contra transacciones_bancarias
        # nunca limpia el 100% por desfase de fecha y montos agrupados.
        # Pendientes de banco sale 100% de banco_historicos_pendientes; subir
        # una hoja nueva reemplaza esos históricos. sess_* quedan ignorados.
        neto_pend_total = saldo_pc_actual["neto_pendientes"]
        saldo_pc_actual["neto_pendientes_total"] = neto_pend_total
        saldo_pc_actual["pendientes_banco_total_creditos"] = saldo_pc_actual["pendientes_banco_creditos"]
        saldo_pc_actual["pendientes_banco_total_debitos"] = saldo_pc_actual["pendientes_banco_debitos"]
        saldo_pc_actual["n_pendientes_banco_total"] = saldo_pc_actual["n_pendientes"]

        saldo_pc_actual["saldo_banco_esperado"] = round(
            saldo_pc_actual["saldo_si_concilio_todo"] + neto_pend_total, 2
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
