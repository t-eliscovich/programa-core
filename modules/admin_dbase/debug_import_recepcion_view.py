"""/admin/debug-import-recepcion — ¿cuánto de cada importación entró de verdad?

TMT 2026-07-31. La dueña: *"al stock están pistoleando hilo, entonces todavía
no cargó todo el stock para contrarrestar ese pago de plata"*.

Hoy el anticipo de una importación se saca del activo ENTERO en cuanto Asinfo
le pone fecha de recepción (`anticipos_con_mercaderia_recibida`). Si la
recepción es PARCIAL —y `recepcion_proveedor.indicador_tiene_recepcion_parcial`
dice que Asinfo la modela— el activo se va de golpe mientras el stock entra de a
poco: un escalón en la utilidad.

Este endpoint es SOLO LECTURA y sirve para medir el desfase antes de tocar el
balance. Por cada importación reciente:

    kg_factura      Σ detalle_factura_proveedor.cantidad     (lo comprado)
    kg_recepcion    Σ detalle_recepcion_proveedor.cantidad   (lo recibido)
    kg_saldo_lote   Σ saldo del último snapshot de los lotes de esa recepción
                    (lo que hoy figura en bodega — el "pistoleado")
    parcial         recepcion_proveedor.indicador_tiene_recepcion_parcial

Modos:
    ?n=40           top N importaciones (default 40, máx 400)
    ?mes=2026-07    sólo las de ese mes de recepción
    ?im=IM-0000537  el detalle línea por línea de una importación
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_import_recepcion",
    __name__,
    url_prefix="/admin/debug-import-recepcion",
)

_MES_RE = re.compile(r"^\d{4}-\d{2}$")
_IM_RE = re.compile(r"^[A-Za-z0-9_\-]{1,30}$")


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return _json({"ok": False, "error": "Metabase no configurado"})

    # ---- detalle de UNA importación ------------------------------------
    im = (request.args.get("im") or "").strip()
    if im:
        if not _IM_RE.match(im):
            return _json({"ok": False, "error": "im invalido"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 300
                   dfp.id_detalle_factura_proveedor    AS id_det,
                   p.codigo                            AS producto,
                   dfp.cantidad                        AS kg_factura,
                   ISNULL(r.kg_rec, 0)                 AS kg_recepcion,
                   ISNULL(r.n_lineas, 0)               AS n_lineas_rec
              FROM factura_proveedor fp
              JOIN detalle_factura_proveedor dfp
                ON dfp.id_factura_proveedor = fp.id_factura_proveedor
              LEFT JOIN producto p ON p.id_producto = dfp.id_producto
              OUTER APPLY (
                   SELECT SUM(ISNULL(drp.cantidad, 0)) AS kg_rec,
                          COUNT(*)                     AS n_lineas
                     FROM detalle_recepcion_proveedor drp
                    WHERE drp.id_detalle_factura_proveedor
                          = dfp.id_detalle_factura_proveedor
              ) r
             WHERE fp.numero = '{im}'
             ORDER BY dfp.id_detalle_factura_proveedor
        """, max_results=300)
        return _json({"ok": True, "im": im, "n": len(rows), "lineas": rows})

    # ---- resumen por importación ---------------------------------------
    try:
        n = max(1, min(400, int(request.args.get("n") or 40)))
    except (TypeError, ValueError):
        n = 40

    mes = (request.args.get("mes") or "").strip()
    filtro_mes = ""
    if mes:
        if not _MES_RE.match(mes):
            return _json({"ok": False, "error": "mes invalido (YYYY-MM)"}, 400)
        filtro_mes = (
            f" AND rp.fecha >= '{mes}-01'"
            f" AND rp.fecha <  DATEADD(month, 1, '{mes}-01')"
        )

    sql = f"""
        SELECT TOP {n}
               fp.numero                          AS im_numero,
               CONVERT(varchar, fp.fecha, 23)     AS fecha_factura,
               CONVERT(varchar, rp.fecha, 23)     AS fecha_recepcion,
               rp.numero                          AS bod,
               rp.estado                          AS estado_rec,
               rp.indicador_tiene_recepcion_parcial AS parcial,
               CONVERT(varchar, rp.fecha_creacion, 120)     AS rec_creada,
               CONVERT(varchar, rp.fecha_modificacion, 120) AS rec_modificada,
               ISNULL(f.kg_factura, 0)            AS kg_factura,
               ISNULL(r.kg_recepcion, 0)          AS kg_recepcion,
               ISNULL(s.kg_saldo_lote, 0)         AS kg_saldo_lote,
               ISNULL(r.n_lotes, 0)               AS n_lotes
          FROM factura_proveedor_importacion fpi
          JOIN factura_proveedor fp
            ON fp.id_factura_proveedor = fpi.id_factura_proveedor
          LEFT JOIN recepcion_proveedor rp
            ON rp.id_recepcion_proveedor = fp.id_recepcion_proveedor
          OUTER APPLY (
               SELECT SUM(ISNULL(dfp.cantidad, 0)) AS kg_factura
                 FROM detalle_factura_proveedor dfp
                WHERE dfp.id_factura_proveedor = fp.id_factura_proveedor
          ) f
          OUTER APPLY (
               SELECT SUM(ISNULL(drp.cantidad, 0))     AS kg_recepcion,
                      COUNT(DISTINCT drp.id_lote)      AS n_lotes
                 FROM detalle_recepcion_proveedor drp
                 JOIN detalle_factura_proveedor dfp2
                   ON dfp2.id_detalle_factura_proveedor
                      = drp.id_detalle_factura_proveedor
                WHERE dfp2.id_factura_proveedor = fp.id_factura_proveedor
          ) r
          OUTER APPLY (
               SELECT SUM(u.saldo) AS kg_saldo_lote
                 FROM (
                       SELECT spl.id_lote, spl.saldo,
                              ROW_NUMBER() OVER (
                                  PARTITION BY spl.id_producto, spl.id_bodega,
                                               spl.id_lote
                                  ORDER BY spl.fecha DESC,
                                           spl.id_saldo_producto_lote DESC
                              ) AS rn
                         FROM saldo_producto_lote spl
                        WHERE spl.id_lote IN (
                              SELECT drp2.id_lote
                                FROM detalle_recepcion_proveedor drp2
                                JOIN detalle_factura_proveedor dfp3
                                  ON dfp3.id_detalle_factura_proveedor
                                     = drp2.id_detalle_factura_proveedor
                               WHERE dfp3.id_factura_proveedor
                                     = fp.id_factura_proveedor
                        )
                 ) u
                WHERE u.rn = 1
          ) s
         WHERE 1 = 1{filtro_mes}
         ORDER BY fpi.id_factura_proveedor DESC
    """
    rows, ok = mc.fetch_dataset_estado(2, sql, max_results=n)
    out = []
    tot_f = tot_r = tot_s = 0.0
    for r in rows:
        kg_f = _f(r.get("kg_factura"))
        kg_r = _f(r.get("kg_recepcion"))
        kg_s = _f(r.get("kg_saldo_lote"))
        tot_f += kg_f
        tot_r += kg_r
        tot_s += kg_s
        out.append({
            "im": str(r.get("im_numero") or "").strip(),
            "fecha_factura": r.get("fecha_factura"),
            "fecha_recepcion": r.get("fecha_recepcion"),
            "bod": str(r.get("bod") or "").strip(),
            "estado_rec": r.get("estado_rec"),
            "parcial": r.get("parcial"),
            "rec_creada": r.get("rec_creada"),
            "rec_modificada": r.get("rec_modificada"),
            "kg_factura": kg_f,
            "kg_recepcion": kg_r,
            "kg_saldo_lote": kg_s,
            "n_lotes": r.get("n_lotes"),
            "falta_kg": round(kg_f - kg_r, 2),
            "pct_recibido": (round(100.0 * kg_r / kg_f, 1) if kg_f else None),
        })
    return _json({
        "ok": ok,
        "n": len(out),
        "mes": mes or None,
        "totales": {
            "kg_factura": round(tot_f, 2),
            "kg_recepcion": round(tot_r, 2),
            "kg_saldo_lote": round(tot_s, 2),
            "falta_kg": round(tot_f - tot_r, 2),
        },
        "importaciones": out,
    })
