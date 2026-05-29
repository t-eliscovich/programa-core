"""Endpoint /admin/debug-yy — diagnostica el display-time YY (TMT 2026-05-28).

Corre buscar(tab='yy') + _aplicar_display_time_yy fila por fila aislada,
y devuelve un JSON con qué fila (si alguna) tira excepción y su traceback.

Sin esto, los 500 de /posdat?tab=yy quedan opacos — el log queda en el
EC2 pero la dueña no tiene shell para verlo.
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import date

from flask import Blueprint, Response

from auth import requiere_login, requiere_permiso

_LOG = logging.getLogger("programa_core.admin_dbase.debug_yy")

bp = Blueprint(
    "admin_debug_yy",
    __name__,
    url_prefix="/admin/debug-yy",
)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def diagnose():
    """JSON con el estado de cada fila YY + cualquier traceback."""
    out: dict = {"ok": True, "rows": [], "errors": []}

    # 1. ¿Existe la columna?
    try:
        from modules.posdat import queries as pq
        out["baseline_col_exists"] = pq._baseline_col_exists()
    except Exception:
        out["baseline_col_exists"] = "ERROR: " + traceback.format_exc()

    # 2. Buscar las filas RAW (sin display-time) para tener el dataset crudo.
    import db
    try:
        baseline_col = ", pd.baseline_date" if out.get("baseline_col_exists") else ""
        raw_rows = db.fetch_all(
            f"""
            SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
                   pd.banc, pd.concepto, pd.clave{baseline_col}
              FROM scintela.posdat pd
             WHERE UPPER(COALESCE(pd.prov, '')) = 'YY'
               AND COALESCE(pd.banc, 0) = 0
               AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
             ORDER BY pd.id_posdat
            """
        ) or []
        out["n_yy_rows"] = len(raw_rows)
    except Exception:
        out["ok"] = False
        out["errors"].append({"step": "fetch raw rows", "tb": traceback.format_exc()})
        return Response(json.dumps(out, indent=2, default=str), mimetype="application/json")

    # 3. Para cada fila YY, simular el cálculo display-time aislado.
    hoy = date.today()
    from modules.posdat import queries as pq
    for raw in raw_rows:
        cell = {
            "id_posdat": raw.get("id_posdat"),
            "concepto": raw.get("concepto"),
            "importe_pers": float(raw.get("importe") or 0),
            "baseline_date": str(raw.get("baseline_date")) if raw.get("baseline_date") else None,
        }
        try:
            # cuota_diaria de provisiones (best-effort)
            cd = None
            try:
                provs = db.fetch_all(
                    "SELECT concepto, importe FROM scintela.provisiones"
                ) or []
                cn = (raw.get("concepto") or "").strip().upper()
                for pr in provs:
                    cp = (pr.get("concepto") or "").strip().upper()
                    if len(cn) >= 3 and len(cp) >= 3 and (cn.startswith(cp) or cp.startswith(cn)):
                        cd = float(pr.get("importe") or 0)
                        break
            except Exception:
                cd = None
            cell["cuota_diaria_resolved"] = cd

            # Probar el helper directo con un dict de 1 fila.
            fila = dict(raw)
            fila["cuota_diaria"] = cd
            pq._aplicar_display_time_yy([fila], hoy=hoy)
            cell["after_display_time"] = {
                "importe": fila.get("importe"),
                "importe_base": fila.get("importe_base"),
                "dias_offset": fila.get("dias_offset"),
                "baseline_date_post": str(fila.get("baseline_date")),
                "yy_cerrado_lazy": fila.get("yy_cerrado_lazy", False),
            }
            cell["status"] = "OK"
        except Exception:
            cell["status"] = "ERROR"
            cell["traceback"] = traceback.format_exc()
            out["ok"] = False
            out["errors"].append({
                "step": f"display_time id_posdat={raw.get('id_posdat')}",
                "tb": traceback.format_exc(),
            })
        out["rows"].append(cell)

    return Response(json.dumps(out, indent=2, default=str), mimetype="application/json")
