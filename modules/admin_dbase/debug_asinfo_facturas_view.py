"""Endpoint /admin/debug-asinfo-facturas — inspeccion READ-ONLY de facturas en Asinfo.

TMT 2026-06-12 (investigacion duena): hay facturas que Asinfo emitio y que
NUNCA se tipearon en el dBase (177714, 177712, 177711, 177710, 177709,
177708, 177645, 175512, 176061 + la 176612 tipeada con cliente equivocado).
La pregunta es si tienen ALGO en comun dentro del ERP (vendedor, punto de
emision/serie SRI, usuario emisor, estado, forma de pago, horario, modulo).

Este endpoint corre queries nativas SOLO LECTURA contra Asinfo (Database 2
de Metabase) via metabase_client.fetch_dataset. No toca datos de negocio
de ningun lado. Modos (query params, todos GET):

    ?meta=<tabla>          — columnas de la tabla (INFORMATION_SCHEMA).
    ?numeros=177714,176061 — fc.* de factura_cliente cuyo numero termina
                             en esos 6 digitos (RIGHT(numero, 6) IN ...).
    ?dia=2026-06-04        — fc.* de todas las facturas de ese dia
                             (para comparar contra contemporaneas).
    ?tabla=X&col=Y&vals=.. — lookup generico TOP 200 * FROM X WHERE Y IN
                             (vals). Identificadores sanitizados a
                             [A-Za-z0-9_], valores a alfanumericos/guion.

Todo identifier/valor se sanitiza antes de interpolar (no hay SQL del
usuario crudo). Gated con el mismo decorator que el resto de /admin/*.
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_asinfo_facturas",
    __name__,
    url_prefix="/admin/debug-asinfo-facturas",
)

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VAL_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,40}$")


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
        # 200 a proposito: el smoke de rutas de CI corre sin Metabase y
        # cualquier 5xx en un GET estatico rompe la suite. fail-soft.
        return _json({"ok": False, "error": "Metabase no configurado"})

    # --- Modo meta: columnas de una tabla -------------------------------
    meta = (request.args.get("meta") or "").strip()
    if meta:
        if not _IDENT_RE.match(meta):
            return _json({"ok": False, "error": "tabla invalida"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME = '{meta}'
             ORDER BY ORDINAL_POSITION
        """, max_results=400)
        return _json({"ok": True, "tabla": meta, "columnas": rows})

    # --- Modo tablas: descubrir nombres de tabla ------------------------
    tablas = (request.args.get("tablas") or "").strip()
    if tablas:
        if not _IDENT_RE.match(tablas):
            return _json({"ok": False, "error": "patron invalido"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT TABLE_NAME, TABLE_TYPE
              FROM INFORMATION_SCHEMA.TABLES
             WHERE TABLE_NAME LIKE '%{tablas}%'
             ORDER BY TABLE_NAME
        """, max_results=400)
        return _json({"ok": True, "patron": tablas, "tablas": rows})

    # --- Modo like: facturas cuyo numero contiene el patron -------------
    like = (request.args.get("like") or "").strip()
    if like:
        pats = sorted({p.strip() for p in like.split(",") if p.strip()})
        if not pats or not all(re.fullmatch(r"[0-9\-]{3,20}", p) for p in pats):
            return _json({"ok": False, "error": "like invalido"}, 400)
        conds = " OR ".join(f"fc.numero LIKE '%{p}%'" for p in pats)
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 100 fc.*
              FROM dbo.factura_cliente fc
             WHERE {conds}
             ORDER BY fc.numero
        """, max_results=100)
        return _json({"ok": True, "like": pats, "n": len(rows), "facturas": rows})

    # --- Modo numeros: fc.* por sufijo de numero SRI --------------------
    numeros = (request.args.get("numeros") or "").strip()
    if numeros:
        nums = sorted({n.strip() for n in numeros.split(",") if n.strip()})
        if not nums or not all(re.fullmatch(r"\d{1,9}", n) for n in nums):
            return _json({"ok": False, "error": "numeros invalidos"}, 400)
        in_list = ", ".join(f"'{n.zfill(6)[-6:]}'" for n in nums)
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 100 fc.*
              FROM dbo.factura_cliente fc
             WHERE RIGHT(fc.numero, 6) IN ({in_list})
             ORDER BY fc.numero
        """, max_results=100)
        return _json({"ok": True, "numeros": nums, "n": len(rows), "facturas": rows})

    # --- Modo dia: todas las facturas de un dia -------------------------
    dia = (request.args.get("dia") or "").strip()
    if dia:
        if not _DATE_RE.match(dia):
            return _json({"ok": False, "error": "dia invalido (YYYY-MM-DD)"}, 400)
        doc = (request.args.get("doc") or "").strip()
        filtro_doc = ""
        if doc:
            if not re.fullmatch(r"\d{1,6}", doc):
                return _json({"ok": False, "error": "doc invalido"}, 400)
            filtro_doc = f" AND fc.id_documento = {int(doc)}"
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 200 fc.*
              FROM dbo.factura_cliente fc
             WHERE CONVERT(date, fc.fecha) = '{dia}'{filtro_doc}
             ORDER BY fc.numero DESC
        """, max_results=200)
        return _json({"ok": True, "dia": dia, "n": len(rows), "facturas": rows})

    # --- Modo lookup generico -------------------------------------------
    tabla = (request.args.get("tabla") or "").strip()
    col = (request.args.get("col") or "").strip()
    vals = (request.args.get("vals") or "").strip()
    if tabla and col and vals:
        if not (_IDENT_RE.match(tabla) and _IDENT_RE.match(col)):
            return _json({"ok": False, "error": "identificador invalido"}, 400)
        vlist = sorted({v.strip() for v in vals.split(",") if v.strip()})
        if not vlist or not all(_VAL_RE.match(v) for v in vlist):
            return _json({"ok": False, "error": "vals invalidos"}, 400)
        in_list = ", ".join(f"'{v}'" for v in vlist)
        rows = mc.fetch_dataset(2, f"""
            SELECT TOP 200 *
              FROM dbo.{tabla}
             WHERE {col} IN ({in_list})
        """, max_results=200)
        return _json({"ok": True, "tabla": tabla, "col": col,
                      "vals": vlist, "n": len(rows), "rows": rows})

    return _json({
        "ok": True,
        "uso": {
            "?meta=factura_cliente": "columnas de la tabla",
            "?numeros=177714,176061": "fc.* por sufijo de numero SRI",
            "?dia=2026-06-04": "fc.* de todas las facturas del dia",
            "?tabla=empresa&col=id_empresa&vals=1,2": "lookup generico",
        },
    })
