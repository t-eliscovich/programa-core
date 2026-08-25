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

# ---------------------------------------------------------------------------
# El importe de la factura: por RENGLÓN o por CABECERA
# ---------------------------------------------------------------------------
# TMT 2026-08-25 (dueña), mirando la 182573: el bloque "Qué se llevó" decía
# 2.064,48 y el Importe de la ficha, en la misma pantalla, 2.064,49.
#
# Son dos cuentas de la misma factura. La card aplica el IVA RENGLÓN POR
# RENGLÓN (`precio × cantidad × (1+iva) − descuento × (1+iva)`) y suma; la
# cabecera de Asinfo —lo que dice el papel y lo que se le manda al SRI— guarda
# `total`, `descuento` e `impuesto` por separado. Redondear doce veces y sumar
# no da lo mismo que sumar y redondear una.
#
# Medido sobre 472 documentos del 19 al 25/08, de los cinco tipos que entran
# (7, 17, 20, 251, 451): 136 difieren y NINGUNO por más de UN centavo. Las NC
# financieras (17) coinciden en las 23. O sea que cambiar de una cuenta a la
# otra no mueve ningún número más allá del centavo — y deja el importe igual
# al del papel, que es lo que el cliente paga.
#
# El síntoma que lo destapó: 90 facturas abiertas con dos centavos o menos
# (USD 0,94). Ver `/admin/facturas-centavos`, que barre las viejas; esto
# arregla las que entren de acá en adelante.
_USD_SUMA_RE = re.compile(
    r"CAST\(\s*SUM\(\s*d\.usd_line\s*\)\s*AS\s+DECIMAL\(\s*18\s*,\s*2\s*\)\s*\)",
    re.IGNORECASE,
)
_USD_LINE_FIN = "END AS usd_line"
_USD_CAB = """,
        -- El importe de la CABECERA: lo mismo que dice el papel. Ver el
        -- comentario de _USD_SUMA_RE en debug_asinfo_facturas_view.py.
        CASE WHEN fc.id_documento IN (17, 20, 451, 501, 652)
             THEN -1 * (ISNULL(fc.total, 0) - ISNULL(fc.descuento, 0) + ISNULL(fc.impuesto, 0))
             ELSE        ISNULL(fc.total, 0) - ISNULL(fc.descuento, 0) + ISNULL(fc.impuesto, 0)
        END AS usd_cab"""
_USD_CAB_OUT = "CAST(MAX(d.usd_cab) AS DECIMAL(18,2))"


def _sql_importe_de_cabecera(sql: str):
    """(sql_nuevo, error). `error` vacío y `sql_nuevo` None = ya estaba hecho."""
    if not sql:
        return None, "la card no tiene SQL"
    if "usd_cab" in sql:
        return None, ""
    if _USD_LINE_FIN not in sql:
        return None, f"no encontre `{_USD_LINE_FIN}` en la card"
    if not _USD_SUMA_RE.search(sql):
        return None, "no encontre el SUM(d.usd_line) del SELECT de afuera"
    nuevo = sql.replace(_USD_LINE_FIN, _USD_LINE_FIN + _USD_CAB, 1)
    nuevo = _USD_SUMA_RE.sub(lambda _m: _USD_CAB_OUT, nuevo, count=1)
    return nuevo, ""


def _card_facturas_get(card_id: str | None = None):
    """Baja una card de Metabase por API. → (card, sql, path, err).

    Sin `card_id` trae la de facturas (ASINFO_CARD_FACTURAS, 199 por default).
    Con `card_id` trae la que se pida: sirve para LEER la consulta de cualquier
    card sin entrar a Metabase — p.ej. la 202, la de retenciones, para saber
    qué columnas trae de verdad y sobre qué fecha filtra. TMT 2026-08-07: la
    dueña preguntó si la retención dice en algún lado si está pagada, y la
    respuesta estaba en el SQL de la card. Es read-only.
    """
    import os

    import requests

    from modules._lib import metabase_client as mc

    url = (os.environ.get("METABASE_URL") or "").strip().rstrip("/")
    card_id = (str(card_id).strip() if card_id
               else (os.environ.get("ASINFO_CARD_FACTURAS") or "199").strip())
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


@bp.route("/card-sql", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def card_sql():
    """La consulta SQL de una card de Asinfo, tal cual está en Metabase.

    `?card=202` (retenciones), `?card=199` (facturas)… Read-only: sólo muestra
    el SQL y los nombres de las columnas que devuelve. Sirve para contestar
    "¿este dato existe del otro lado?" sin adivinar ni pedirle a nadie que
    entre a Metabase.
    """
    card_id = (request.args.get("card") or "").strip() or None
    card, sql, path, err = _card_facturas_get(card_id)
    if err:
        return _json({"ok": False, "card_id": card_id, "error": err})
    cols = [c.get("name") for c in ((card or {}).get("result_metadata") or [])]
    return _json({
        "ok": True,
        "card_id": (card or {}).get("id"),
        "card_name": (card or {}).get("name"),
        "columnas_que_devuelve": cols,
        "sql_path": path,
        "sql": sql,
    })


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


@bp.route("/card-importe", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def card_importe():
    """De dónde saca la card el importe: de los renglones o de la cabecera."""
    card, sql, path, err = _card_facturas_get()
    if err:
        return _json({"ok": False, "error": err})
    ya = "usd_cab" in (sql or "")
    _nuevo, problema = _sql_importe_de_cabecera(sql or "")
    return _json({
        "ok": True,
        "card_id": (card or {}).get("id"),
        "sql_path": path,
        "importe_desde_la_cabecera": ya,
        "se_puede_cambiar": bool(_nuevo),
        "problema": problema or None,
        "como_aplicar": "POST /admin/debug-asinfo-facturas/card-importe/fix",
    })


@bp.route("/card-importe/fix", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def card_importe_fix():
    """Hace que el importe salga de la cabecera. Idempotente."""
    import os

    import requests

    from modules._lib import metabase_client as mc

    card, sql, path, err = _card_facturas_get()
    if err:
        return _json({"ok": False, "error": err}, 502)
    nuevo, problema = _sql_importe_de_cabecera(sql or "")
    if nuevo is None:
        if problema:
            return _json({"ok": False, "error": problema}, 422)
        return _json({"ok": True, "noop": True,
                      "msg": "el importe YA sale de la cabecera"})

    dq = card.get("dataset_query") or {}
    if path == "native.query":
        dq["native"]["query"] = nuevo
    else:
        dq["stages"][0]["native"] = nuevo

    url = (os.environ.get("METABASE_URL") or "").strip().rstrip("/")
    card_id = (os.environ.get("ASINFO_CARD_FACTURAS") or "199").strip()
    token = mc._session_token or mc._login(requests)
    r = requests.put(
        f"{url}/api/card/{card_id}",
        json={
            "name": card.get("name"),
            "dataset_query": dq,
            "display": card.get("display"),
            "description": card.get("description"),
            "visualization_settings": card.get("visualization_settings") or {},
        },
        headers={"X-Metabase-Session": token, "Content-Type": "application/json"},
        timeout=20,
    )
    if r.status_code >= 400:
        return _json({"ok": False, "error": f"PUT -> HTTP {r.status_code}",
                      "body": r.text[:500]}, 502)

    from modules.asinfo import service as asinfo_service
    asinfo_service.reset_facturas_cache()

    _c2, sql_check, _p2, _e2 = _card_facturas_get()
    return _json({
        "ok": True,
        "verificado_en_card": "usd_cab" in (sql_check or ""),
        "cache_facturas_reseteado": True,
    })
