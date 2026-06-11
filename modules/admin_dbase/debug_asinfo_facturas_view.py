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


# ---------------------------------------------------------------------------
# /card-estado — ver y corregir el filtro fc.estado de la card de facturas
# ---------------------------------------------------------------------------
# TMT 2026-06-11 "facturas fantasma": la card ASINFO_CARD_FACTURAS (199)
# incluia fc.estado = 0 en su WHERE. En Asinfo estado=0 = emision NO
# autorizada por el SRI que se re-emitio con otro numero — el dBase tipea
# la version corregida, PC importaba LAS DOS → doble conteo de kg.
# GET  /card-estado      → muestra la lista actual de estados (read-only).
# POST /card-estado/fix  → saca el 0 de `fc.estado IN (...)` via
#                          PUT /api/card/<id> y resetea el cache del bridge.
# Solo toca ese fragmento del SQL; cualquier otra cosa de la card queda igual.

_ESTADO_IN_RE = re.compile(
    r"(fc\.estado\s+IN\s*\(\s*)([0-9,\s]+)(\s*\))", re.IGNORECASE
)


def _card_facturas_get():
    """Baja la card ASINFO_CARD_FACTURAS por API. → (card, sql, path, err)."""
    import os

    import requests

    from modules._lib import metabase_client as mc

    url = (os.environ.get("METABASE_URL") or "").strip().rstrip("/")
    card_id = (os.environ.get("ASINFO_CARD_FACTURAS") or "199").strip()
    token = mc._session_token or mc._login(requests)
    if not (url and token):
        return None, None, None, "Metabase no configurado o login fallo"
    r = requests.get(
        f"{url}/api/card/{card_id}",
        headers={"X-Metabase-Session": token},
        timeout=20,
    )
    if r.status_code >= 400:
        return None, None, None, f"GET card {card_id} -> HTTP {r.status_code}"
    card = r.json()
    dq = card.get("dataset_query") or {}
    nat = dq.get("native") or {}
    if isinstance(nat, dict) and nat.get("query"):
        return card, nat["query"], "native.query", None
    stages = dq.get("stages") or []
    if stages and isinstance((stages[0] or {}).get("native"), str):
        return card, stages[0]["native"], "stages[0].native", None
    return card, None, None, "no encontre SQL nativo en la card"


@bp.route("/card-estado", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def card_estado():
    card, sql, path, err = _card_facturas_get()
    if err:
        return _json({"ok": False, "error": err})
    m = _ESTADO_IN_RE.search(sql or "")
    lista = re.findall(r"\d+", m.group(2)) if m else None
    return _json({
        "ok": True,
        "card_id": (card or {}).get("id"),
        "card_name": (card or {}).get("name"),
        "sql_path": path,
        "estado_in": lista,
        "incluye_estado_0": bool(lista and "0" in lista),
        "fragmento": m.group(0) if m else "(fc.estado IN no encontrado)",
        "sql": sql,
    })


@bp.route("/card-estado/fix", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def card_estado_fix():
    import os

    import requests

    from modules._lib import metabase_client as mc

    card, sql, path, err = _card_facturas_get()
    if err:
        return _json({"ok": False, "error": err}, 502)
    m = _ESTADO_IN_RE.search(sql or "")
    if not m:
        return _json({"ok": False, "error": "fc.estado IN (...) no encontrado",
                      "sql": sql}, 422)
    lista = re.findall(r"\d+", m.group(2))
    if "0" not in lista:
        return _json({"ok": True, "noop": True,
                      "msg": "la card ya NO incluye estado 0",
                      "estado_in": lista})
    nueva = [x for x in lista if x != "0"]
    frag_nuevo = "fc.estado IN (" + ", ".join(nueva) + ")"
    sql_new = _ESTADO_IN_RE.sub(lambda _mm: frag_nuevo, sql, count=1)

    dq = card.get("dataset_query") or {}
    if path == "native.query":
        dq["native"]["query"] = sql_new
    else:
        dq["stages"][0]["native"] = sql_new

    url = (os.environ.get("METABASE_URL") or "").strip().rstrip("/")
    card_id = (os.environ.get("ASINFO_CARD_FACTURAS") or "199").strip()
    token = mc._session_token or mc._login(requests)
    body = {
        "name": card.get("name"),
        "dataset_query": dq,
        "display": card.get("display"),
        "description": card.get("description"),
        "visualization_settings": card.get("visualization_settings") or {},
    }
    r = requests.put(
        f"{url}/api/card/{card_id}",
        json=body,
        headers={"X-Metabase-Session": token, "Content-Type": "application/json"},
        timeout=20,
    )
    if r.status_code >= 400:
        return _json({"ok": False, "error": f"PUT -> HTTP {r.status_code}",
                      "body": r.text[:500]}, 502)

    # invalidar cache del bridge para que /facturas/desde-asinfo lo vea ya
    from modules.asinfo import service as asinfo_service
    asinfo_service.reset_facturas_cache()

    # verificar re-leyendo la card
    _, sql_check, _, _ = _card_facturas_get()
    m2 = _ESTADO_IN_RE.search(sql_check or "")
    lista_check = re.findall(r"\d+", m2.group(2)) if m2 else None
    return _json({
        "ok": True,
        "antes": "fc.estado IN (" + ", ".join(lista) + ")",
        "despues": frag_nuevo,
        "verificado_en_card": lista_check,
        "cache_facturas_reseteado": True,
    })
