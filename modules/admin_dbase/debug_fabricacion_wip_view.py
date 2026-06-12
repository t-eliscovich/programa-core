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

    # ?sql=<SELECT ...> — query libre SOLO LECTURA contra Asinfo para
    # diagnostico (mismo nivel de confianza que el lookup generico de
    # debug-asinfo-facturas). Guard: un solo statement, empieza con
    # SELECT/WITH, sin keywords de escritura.
    q = (request.args.get("sql") or "").strip()
    if q:
        qu = q.upper()
        prohibidas = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "EXEC",
                      "MERGE", "TRUNCATE", "CREATE", "GRANT", ";")
        if not (qu.startswith("SELECT") or qu.startswith("WITH")):
            return _json({"error": "solo SELECT/WITH"}, 400)
        if any(k in qu for k in prohibidas):
            return _json({"error": "keyword prohibida"}, 400)
        try:
            return _json({"rows": mc.fetch_dataset(2, q, max_results=500)})
        except Exception as e:  # noqa: BLE001
            return _json({"error": str(e)[:500]}, 500)

    # ?v=ing — ingresos REALES de fabricacion via kardex:
    # movimiento_inventario (id_orden_fabricacion NOT NULL, bodega_destino=b)
    # + detalle. Devuelve ingresos por anio y el WIP global Excel-style:
    # issued(OSM todas las ordenes) - ingresos(kardex).
    if (request.args.get("v") or "") == "ing":
        out = {}
        for b in bodegas:
            sql_ing = f"""
                SELECT YEAR(m.fecha) AS anio, COUNT(DISTINCT m.id_movimiento_inventario) AS n_movs,
                       SUM(ISNULL(d.cantidad, 0)) AS kg
                  FROM movimiento_inventario m
                  JOIN detalle_movimiento_inventario d
                    ON d.id_movimiento_inventario = m.id_movimiento_inventario
                  JOIN orden_fabricacion o
                    ON o.id_orden_fabricacion = m.id_orden_fabricacion
                 WHERE o.id_bodega = {int(b)}
                   AND m.id_bodega_destino = {int(b)}
                 GROUP BY YEAR(m.fecha)
                 ORDER BY YEAR(m.fecha)
            """
            ing = mc.fetch_dataset(2, sql_ing, max_results=100)
            sql_osm = f"""
                SELECT YEAR(m2.fecha_creacion) AS anio,
                       SUM(ISNULL(d.cantidad_despachada, 0)) AS kg
                  FROM detalle_orden_salida_material_orden_fabricacion j
                  JOIN detalle_orden_salida_material d
                    ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
                  JOIN orden_fabricacion o
                    ON o.id_orden_fabricacion = j.id_orden_fabricacion
                  LEFT JOIN orden_salida_material m2
                    ON m2.id_orden_salida_material = d.id_orden_salida_material
                 WHERE o.id_bodega = {int(b)}
                 GROUP BY YEAR(m2.fecha_creacion)
                 ORDER BY YEAR(m2.fecha_creacion)
            """
            try:
                osm = mc.fetch_dataset(2, sql_osm, max_results=100)
            except Exception as e:  # noqa: BLE001
                osm = [{"error": str(e)[:300]}]
            out[f"bodega_{b}"] = {"ingresos_kardex_por_anio": ing, "osm_por_anio": osm}
        return _json(out)

    # ?v=mov — explora movimiento_fabricacion (candidato a "Ingresos de
    # Fabricacion" del Excel): totales por operacion x anio por bodega, y
    # WIP global = issued(OSM, todas las ordenes) - ingresos(mov).
    if (request.args.get("v") or "") == "mov":
        out = {}
        for b in bodegas:
            sql = f"""
                SELECT operacion, YEAR(fecha_creacion) AS anio,
                       COUNT(*) AS n, SUM(ISNULL(cantidad,0)) AS kg
                  FROM movimiento_fabricacion
                 WHERE id_bodega = {int(b)}
                 GROUP BY operacion, YEAR(fecha_creacion)
                 ORDER BY operacion, anio
            """
            out[f"bodega_{b}_mov_por_operacion_anio"] = mc.fetch_dataset(2, sql, max_results=200)
        return _json(out)

    # ?desde=YYYY-MM-DD — filtra ordenes por o.fecha >= desde (para encontrar
    # el corte que usa el reporte nube). ?por=anio|mes (con &anio=YYYY) —
    # agrupa por anio/mes de o.fecha en vez de estado.
    desde = (request.args.get("desde") or "").strip()
    if desde and not re.match(r"^\d{4}-\d{2}-\d{2}$", desde):
        desde = ""
    por = (request.args.get("por") or "").strip()
    try:
        anio = int(request.args.get("anio") or 0)
    except (TypeError, ValueError):
        anio = 0

    grp = "o.estado_produccion"
    grp_alias = "estado"
    extra_where = f" AND fecha >= '{desde}'" if desde else ""
    if por == "anio":
        grp, grp_alias = "YEAR(o.fecha)", "anio"
    elif por == "mes" and anio:
        grp, grp_alias = "MONTH(o.fecha)", "mes"
        extra_where += f" AND YEAR(fecha) = {anio}"

    out = {}
    for b in bodegas:
        sql = f"""
            WITH ofs AS (
                SELECT id_orden_fabricacion, estado_produccion, fecha,
                       ISNULL(cantidad, 0) AS planif,
                       ISNULL(cantidad_fabricada, 0) AS fab
                  FROM orden_fabricacion
                 WHERE id_bodega = {int(b)}{extra_where}
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
            SELECT {grp}                                     AS {grp_alias},
                   COUNT(*)                                  AS n_ofts,
                   SUM(CASE WHEN ISNULL(i.issued,0) > 0.005 OR o.fab > 0.005
                            THEN 1 ELSE 0 END)               AS n_con_mov,
                   SUM(ISNULL(i.issued, 0))                  AS issued,
                   SUM(o.fab)                                AS fab,
                   SUM(o.planif)                             AS planif,
                   SUM(ISNULL(i.issued, 0) - o.fab)          AS saldo
              FROM ofs o
              LEFT JOIN issued i ON i.id_orden_fabricacion = o.id_orden_fabricacion
             GROUP BY {grp}
             ORDER BY {grp}
        """
        rows = mc.fetch_dataset(2, sql, max_results=100)
        tot = {"issued": 0.0, "fab": 0.0, "saldo": 0.0}
        tot_sin5 = {"issued": 0.0, "fab": 0.0, "saldo": 0.0}
        for r in rows or []:
            for k in ("issued", "fab", "saldo"):
                v = float(r.get(k) or 0)
                tot[k] += v
                if grp_alias == "estado" and int(r.get("estado") or 0) != 5:
                    tot_sin5[k] += v
        out[f"bodega_{b}"] = {
            f"por_{grp_alias}": rows,
            "total_todos_estados": tot,
            "total_sin_estado_5_(actual_vista)": tot_sin5,
        }
    return _json(out)
