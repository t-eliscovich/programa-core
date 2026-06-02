"""Endpoint /admin/debug-ustock (TMT 2026-06-02).

Diagnóstico del bug "ustock live=0" reportado por /admin/dbase-sync
2026-06-02: el drift mostraba snap=7.8M live=0 y patrimonio 38% off.

El cálculo live de vsto vive en informe_balance() (línea ~3700) y
depende de:
    - hist  (último snapshot scintela.historia)
    - inic  (iniciales_mes_actual)
    - kg_facturas_pc_no_sincronizadas()

Este view tira los 4 inputs en crudo + la simulación final, todo en
una sola respuesta JSON. No requiere SSH ni SSM.
"""
from __future__ import annotations

import json
import traceback
from datetime import date

from flask import Blueprint, Response

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_ustock",
    __name__,
    url_prefix="/admin/debug-ustock",
)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def diagnose():
    out: dict = {"ok": True, "hoy": str(date.today())}
    hoy = date.today()

    import db

    # 1. Último snapshot historia.
    try:
        out["historia_top3"] = db.fetch_all(
            """
            SELECT fecha, stock, ustock, hilado, tejido, terminado,
                   banco, cart, deuda, patrimonio
            FROM scintela.historia
            ORDER BY fecha DESC
            LIMIT 3
            """
        ) or []
    except Exception:
        out["historia_top3_error"] = traceback.format_exc()

    # 2. Iniciales del mes actual — yy puede ser 2-dig o 4-dig.
    iniciales_actual = {}
    for yy_val in (hoy.year % 100, hoy.year):
        try:
            rows = db.fetch_all(
                """
                SELECT id_iniciales, mesnum, yy, hilado, tejido, terminado,
                       kprog, um, uk, uf
                FROM scintela.iniciales
                WHERE mesnum = %s AND yy = %s
                ORDER BY id_iniciales DESC
                LIMIT 5
                """,
                (hoy.month, yy_val),
            ) or []
            iniciales_actual[f"mes={hoy.month}_yy={yy_val}"] = rows
        except Exception:
            iniciales_actual[f"mes={hoy.month}_yy={yy_val}_error"] = traceback.format_exc()
    out["iniciales_mes_actual_raw"] = iniciales_actual

    # 3. Fallback (más reciente con datos reales).
    try:
        out["iniciales_fallback_top5"] = db.fetch_all(
            """
            SELECT id_iniciales, mesnum, yy, hilado, tejido, terminado,
                   kprog, um, uk, uf
            FROM scintela.iniciales
            WHERE COALESCE(kprog, 0) > 0 OR COALESCE(hilado, 0) > 0
            ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
            LIMIT 5
            """
        ) or []
    except Exception:
        out["iniciales_fallback_error"] = traceback.format_exc()

    # 4. Resumen de la tabla iniciales.
    try:
        out["iniciales_resumen"] = db.fetch_one(
            """
            SELECT COUNT(*) AS n,
                   MIN(yy) AS min_yy, MAX(yy) AS max_yy,
                   MAX(id_iniciales) AS max_id
            FROM scintela.iniciales
            """
        ) or {}
    except Exception:
        out["iniciales_resumen_error"] = traceback.format_exc()

    # 5. kg_facturas_pc_no_sincronizadas — lo que descuenta del terminado.
    try:
        r = db.fetch_one(
            """
            SELECT COALESCE(SUM(kg), 0) AS total,
                   COUNT(*) AS n_facturas
            FROM scintela.factura
            WHERE COALESCE(usuario_crea, '') NOT IN ('dbf-import', 'asinfo-backfill')
              AND (stat IS NULL OR stat <> 'X')
            """
        ) or {}
        out["kg_facturas_pc"] = {
            "total_kg": float(r.get("total") or 0),
            "n_facturas": r.get("n_facturas"),
        }
    except Exception:
        out["kg_facturas_pc_error"] = traceback.format_exc()

    # 6. Simulación del cálculo final.
    sim: dict = {}
    try:
        from modules.informes.queries import (
            informe_balance,
            iniciales_mes_actual,
            historia_ultimo_mes,
            kg_facturas_pc_no_sincronizadas,
        )
        hist = historia_ultimo_mes() or {}
        inic = iniciales_mes_actual() or {}
        kg_pc = kg_facturas_pc_no_sincronizadas()

        h_hilado = float(hist.get("hilado") or 0) if "hilado" in hist else 0.0
        h_tejido_kg = float(hist.get("tejido") or 0) if "tejido" in hist else 0.0
        h_terminado_kg = float(hist.get("terminado") or 0) if "terminado" in hist else 0.0
        used_fallback = False
        if h_hilado == 0 and h_tejido_kg == 0 and h_terminado_kg == 0:
            used_fallback = True
            h_hilado = float(inic.get("hilado") or 0)
            h_tejido_kg = float(inic.get("tejido") or 0)
            h_terminado_kg = float(inic.get("terminado") or 0)

        sim["historia_ultimo_mes"] = {
            k: hist.get(k) for k in
            ("fecha", "stock", "ustock", "hilado", "tejido", "terminado")
        }
        sim["iniciales_mes_actual"] = {
            k: inic.get(k) for k in
            ("id_iniciales", "mesnum", "yy", "hilado", "tejido", "terminado", "um", "uk", "uf")
        }
        sim["pre_descuento"] = {
            "h_hilado": h_hilado,
            "h_tejido_kg": h_tejido_kg,
            "h_terminado_kg": h_terminado_kg,
            "used_fallback_to_iniciales": used_fallback,
        }
        sim["kg_facturas_pc"] = kg_pc
        h_terminado_post = max(0.0, h_terminado_kg - kg_pc)
        sim["post_descuento_terminado"] = h_terminado_post
        sim["TERMINADO_SE_FUE_A_0_POR_KG_PC"] = (
            h_terminado_kg > 0 and h_terminado_post == 0
        )

        bal = informe_balance() or {}
        comp = bal.get("diagnostico", {}).get("componentes", {})
        sim["vsto_final"] = comp.get("vsto")
        sim["componentes_balance"] = {
            k: comp.get(k) for k in
            ("salbanc_total", "cart", "vsto", "totp", "patr")
        }
    except Exception:
        sim["traceback"] = traceback.format_exc()
        out["ok"] = False
    out["sim"] = sim

    return Response(
        json.dumps(out, indent=2, default=str),
        mimetype="application/json",
    )
