"""/admin/debug-hilo-local — ¿qué llegó tarde: la factura, la recepción o el vínculo?

TMT 2026-09-16. Tamara, mirando una compra de hilo local de HY que apareció en
el programa cuatro días después de que el hilo ya estaba en bodega: *"¿por qué
tardó tanto?? medilo"*.

Las fechas que muestra /importaciones son las que se TIPEAN en Asinfo
(`factura_proveedor.fecha`, `recepcion_proveedor.fecha`), no el momento en que
cada fila apareció. Con esas dos solas no se puede saber qué esperó a qué. Lo
que sí lo dice es `fecha_creacion` de cada fila, y eso vive en Asinfo (SQL
Server) — la consola SQL de `/admin/sql` es Postgres y no llega.

Este endpoint es SOLO LECTURA y contesta tres cosas:

    ?cols=<tabla>   las columnas de una tabla de Asinfo (para no adivinar
                    nombres: ¿la recepción tiene proveedor propio o cuelga
                    siempre de la factura?)
    ?n=40           por cada compra local de hilo (bodega 51), CUÁNDO se creó
                    la factura, CUÁNDO la recepción y cuándo se vincularon,
                    contra la fecha en que el programa la cargó
    ?bod=BOD-…      el detalle línea por línea de una recepción

Nada de esto escribe: es el paso previo a mover el disparador del motor de
compras locales de la factura a la recepción.
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_hilo_local",
    __name__,
    url_prefix="/admin/debug-hilo-local",
)

#: Las tablas de Asinfo que este diagnóstico puede describir. Lista blanca
#: EXPLÍCITA: el nombre entra en el SQL, así que no se acepta texto libre.
_TABLAS = {
    "recepcion_proveedor",
    "detalle_recepcion_proveedor",
    "factura_proveedor",
    "detalle_factura_proveedor",
}

_BOD_RE = re.compile(r"^[A-Za-z0-9_\-]{1,30}$")

#: Bodega de hilo (la misma constante que usa el motor de compras locales).
BODEGA_HILO = 51


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
        return _json({"ok": False, "error": "Metabase no configurado"})

    # ---- columnas de una tabla (para no adivinar nombres) ---------------
    tabla = (request.args.get("cols") or "").strip().lower()
    if tabla:
        if tabla not in _TABLAS:
            return _json({"ok": False, "error": "tabla no permitida",
                          "permitidas": sorted(_TABLAS)}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT ORDINAL_POSITION AS pos, COLUMN_NAME AS columna,
                   DATA_TYPE AS tipo, IS_NULLABLE AS acepta_null
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME = '{tabla}'
             ORDER BY ORDINAL_POSITION
        """, max_results=200)
        return _json({"ok": True, "tabla": tabla, "n": len(rows),
                      "columnas": rows})

    # ---- detalle de UNA recepción --------------------------------------
    bod = (request.args.get("bod") or "").strip()
    if bod:
        if not _BOD_RE.match(bod):
            return _json({"ok": False, "error": "bod invalido"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 300
                   rp.numero                                  AS bod,
                   CONVERT(varchar, rp.fecha, 23)             AS fecha_bod,
                   rp.numero_factura                          AS sri_en_el_bod,
                   rp.estado                                  AS estado_bod,
                   CONVERT(varchar, rp.fecha_anulacion, 23)   AS bod_anulado,
                   CONVERT(varchar, rp.fecha_creacion, 120)   AS bod_creada,
                   drp.id_detalle_recepcion_proveedor         AS id_det_rec,
                   drp.id_detalle_factura_proveedor           AS id_det_fact,
                   drp.id_bodega                              AS bodega,
                   p.codigo                                   AS producto,
                   drp.cantidad                               AS kg,
                   fp.numero                                  AS factura_asinfo,
                   fp.numero_factura                          AS nro_sri,
                   CONVERT(varchar, fp.fecha, 23)             AS fecha_factura,
                   CONVERT(varchar, fp.fecha_creacion, 120)   AS factura_creada
              FROM recepcion_proveedor rp
              JOIN detalle_recepcion_proveedor drp
                ON drp.id_recepcion_proveedor = rp.id_recepcion_proveedor
              LEFT JOIN producto p ON p.id_producto = drp.id_producto
              LEFT JOIN detalle_factura_proveedor dfp
                ON dfp.id_detalle_factura_proveedor
                   = drp.id_detalle_factura_proveedor
              LEFT JOIN factura_proveedor fp
                ON fp.id_factura_proveedor = dfp.id_factura_proveedor
             WHERE rp.numero = '{bod}'
             ORDER BY drp.id_detalle_recepcion_proveedor
        """, max_results=300)
        return _json({"ok": True, "bod": bod, "n": len(rows), "lineas": rows})

    # ---- el cuadro de tiempos ------------------------------------------
    try:
        n = max(1, min(200, int(request.args.get("n") or 40)))
    except (TypeError, ValueError):
        n = 40

    # Misma forma que `compras_locales_asinfo`: partimos de la FACTURA (una
    # puede tener N recepciones parciales), pero acá traemos los timestamps
    # de CREACIÓN de las dos puntas, que son los que contestan la pregunta.
    sql = f"""
        SELECT TOP {n}
               fp.numero                                  AS factura_asinfo,
               fp.numero_factura                          AS nro_sri,
               COALESCE(e.nombre_comercial, e.nombre_fiscal, '') AS proveedor,
               e.codigo                                   AS ruc,
               CONVERT(varchar, fp.fecha, 23)             AS fecha_factura,
               CONVERT(varchar, fp.fecha_creacion, 120)   AS factura_creada,
               CONVERT(varchar, MIN(rp.fecha), 23)        AS fecha_recepcion,
               CONVERT(varchar, MIN(rp.fecha_creacion), 120)     AS bod_creada,
               CONVERT(varchar, MAX(rp.fecha_modificacion), 120) AS bod_modificada,
               MIN(rp.numero)                             AS bod,
               -- ⭐ el nº de factura que trae la RECEPCIÓN (columna propia,
               -- NOT NULL). Si viene lleno desde que se crea el BOD, el motor
               -- no necesita esperar a la factura para nada.
               MIN(rp.numero_factura)                     AS sri_en_el_bod,
               MIN(rp.usuario_creacion)                   AS bod_creada_por,
               MAX(CAST(rp.id_empresa AS varchar))        AS id_empresa_bod,
               COUNT(DISTINCT rp.id_recepcion_proveedor)  AS n_recepciones,
               MAX(CAST(rp.indicador_tiene_recepcion_parcial AS int)) AS parcial,
               MIN(rp.estado)                             AS estado_rec,
               SUM(d.cantidad)                            AS kg,
               MIN(p.codigo)                              AS producto
          FROM factura_proveedor fp
          LEFT JOIN factura_proveedor_importacion fpi
                 ON fpi.id_factura_proveedor = fp.id_factura_proveedor
          LEFT JOIN empresa e ON e.id_empresa = fp.id_empresa
          JOIN detalle_factura_proveedor dfp
               ON dfp.id_factura_proveedor = fp.id_factura_proveedor
          JOIN detalle_recepcion_proveedor d
               ON d.id_detalle_factura_proveedor
                  = dfp.id_detalle_factura_proveedor
          JOIN recepcion_proveedor rp
               ON rp.id_recepcion_proveedor = d.id_recepcion_proveedor
          LEFT JOIN producto p ON p.id_producto = d.id_producto
         WHERE fpi.id_factura_proveedor IS NULL
           AND d.id_bodega = {int(BODEGA_HILO)}
           AND fp.fecha >= DATEADD(month, -6, GETDATE())
         GROUP BY fp.id_factura_proveedor, fp.numero, fp.fecha, fp.numero_factura,
                  fp.fecha_creacion, e.nombre_comercial, e.nombre_fiscal, e.codigo
         ORDER BY fp.id_factura_proveedor DESC
    """
    filas = mc.fetch_dataset(2, sql, max_results=n)

    # Al lado, cuándo cargó cada una el programa — así el desfase se lee de
    # una sola pasada, sin cruzar dos pantallas a mano.
    cargadas = _cargadas_en_pc()
    for f in filas or []:
        clave = _clave(f.get("nro_sri"))
        f["pc_cargada"] = cargadas.get(clave, {}).get("crea")
        f["pc_id_compra"] = cargadas.get(clave, {}).get("id_compra")
        f["pc_usuario"] = cargadas.get(clave, {}).get("usuario")

    return _json({"ok": True, "n": len(filas or []), "filas": filas})


def _clave(nro_sri) -> int | None:
    """'001-002-000037649' → 37649 — la misma llave con la que cruza el motor."""
    from modules.asinfo.service import numero_de_factura

    return numero_de_factura(nro_sri or "")


def _cargadas_en_pc() -> dict:
    """{nº de factura → cuándo y quién la cargó} de las compras de hilo en PC."""
    import db

    try:
        rows = db.fetch_all(
            r"""
            SELECT c.id_compra,
                   NULLIF(substring(btrim(COALESCE(c.concepto, ''))
                          FROM '^(\d+)'), '')::bigint AS fact_num,
                   TO_CHAR(c.fecha_crea, 'YYYY-MM-DD HH24:MI:SS') AS crea,
                   c.usuario_crea AS usuario
              FROM scintela.compra c
             WHERE c.tipo = 'H'
               AND COALESCE(c.stat, '') <> 'Y'
               AND c.fecha >= CURRENT_DATE - INTERVAL '6 months'
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- el diagnóstico nunca rompe la pantalla
        return {}
    out: dict = {}
    for r in rows:
        num = r.get("fact_num")
        if num is None:
            continue
        out[int(num)] = {"crea": r.get("crea"), "id_compra": r.get("id_compra"),
                         "usuario": (r.get("usuario") or "").strip()}
    return out
