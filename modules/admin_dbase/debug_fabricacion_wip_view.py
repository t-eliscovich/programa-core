"""/admin/debug-fabricacion-wip — diagnóstico READ-ONLY del stock en proceso.

TMT 2026-06-12 dueña: "PT en proceso está muy bajo, fijate que no estamos
contabilizando que el excel sí". El Excel nube (Saldos Inventarios) dice
WIP PT = 41.630 kg; la vista PC (fabricacion_proceso) da ~12.590.

Hipótesis a confirmar: el filtro `estado_produccion <> 5` excluye órdenes
cerradas cuyo despacho≠ingreso, y/o `cantidad_fabricada` ≠ ingresos reales
de fabricación. Este endpoint desglosa Σ(issued − fab) POR ESTADO de la
orden, para cada bodega (52 TC / 53 PT). Solo lectura vía Metabase.

Modos:
    ?bodega=53            — desglose por estado_produccion (default 52 y 53)
    ?tablas=ingreso       — tablas de Asinfo cuyo nombre matchee el patrón
    ?meta=<tabla>         — columnas de una tabla
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_fabricacion_wip",
    __name__,
    url_prefix="/admin/debug-fabricacion-wip",
)

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return _json({"error": "metabase no disponible en este entorno"})

    tablas = (request.args.get("tablas") or "").strip()
    if tablas and _IDENT_RE.match(tablas):
        sql = (
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_NAME LIKE '%{tablas}%' ORDER BY TABLE_NAME"
        )
        return _json({"sql": sql, "rows": mc.fetch_dataset(2, sql, max_results=500)})

    meta = (request.args.get("meta") or "").strip()
    if meta and _IDENT_RE.match(meta):
        sql = (
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME = '{meta}' ORDER BY ORDINAL_POSITION"
        )
        return _json({"sql": sql, "rows": mc.fetch_dataset(2, sql, max_results=500)})

    try:
        bodegas = [int(request.args.get("bodega"))] if request.args.get("bodega") else [52, 53]
    except (TypeError, ValueError):
        bodegas = [52, 53]

    out = {}
    for b in bodegas:
        sql = f"""
            WITH ofs AS (
                SELECT id_orden_fabricacion, estado_produccion,
                       ISNULL(cantidad, 0) AS planif,
                       ISNULL(cantidad_fabricada, 0) AS fab
                  FROM orden_fabricacion
                 WHERE id_bodega = {int(b)}
            ),
            issued AS (
                SELECT j.id_orden_fabricacion,
                       SUM(ISNULL(d.cantidad_despachada, 0)) AS issued
                  FROM detalle_orden_salida_material_orden_fabricacion j
                  JOIN detalle_orden_salida_material d
                    ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
                 WHERE j.id_orden_fabricacion IN (SELECT id_orden_fabricacion FROM ofs)
                 GROUP BY j.id_orden_fabricacion
            )
            SELECT o.estado_produccion                       AS estado,
                   COUNT(*)                                  AS n_ofts,
                   SUM(CASE WHEN ISNULL(i.issued,0) > 0.005 OR o.fab > 0.005
                            THEN 1 ELSE 0 END)               AS n_con_mov,
                   SUM(ISNULL(i.issued, 0))                  AS issued,
                   SUM(o.fab)                                AS fab,
                   SUM(o.planif)                             AS planif,
                   SUM(ISNULL(i.issued, 0) - o.fab)          AS saldo
              FROM ofs o
              LEFT JOIN issued i ON i.id_orden_fabricacion = o.id_orden_fabricacion
             GROUP BY o.estado_produccion
             ORDER BY o.estado_produccion
        """
        rows = mc.fetch_dataset(2, sql, max_results=100)
        tot = {"issued": 0.0, "fab": 0.0, "saldo": 0.0}
        tot_sin5 = {"issued": 0.0, "fab": 0.0, "saldo": 0.0}
        for r in rows or []:
            for k in ("issued", "fab", "saldo"):
                v = float(r.get(k) or 0)
                tot[k] += v
                if int(r.get("estado") or 0) != 5:
                    tot_sin5[k] += v
        out[f"bodega_{b}"] = {
            "por_estado": rows,
            "total_todos_estados": tot,
            "total_sin_estado_5_(actual_vista)": tot_sin5,
        }
    return _json(out)
