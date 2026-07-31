"""/admin/debug-cruce-compras — a qué importación se le atribuye cada compra.

TMT 2026-07-31 (dueña): *"¿por qué la plata de una importación NO recibida está
en la tarifa del hilado?"*.

El caso: al deshacer la conversión de AC 22 (IM-0000578, que NO figura recibida)
la utilidad devolvió los 53.861 del anticipo **pero el stock bajó 49.425**,
porque esa compra estaba dentro del promedio ponderado del hilado. Una compra de
mercadería que no llegó no tendría que estar ahí: `costo_hilado_recibido_mes`
sólo suma importaciones RECIBIDAS en el mes.

La hipótesis que este endpoint prueba o descarta: la compra no se le atribuye a
SU importación sino a **otra**, y si esa otra sí está recibida, su plata entra al
costo del mes. Dos motivos en el código, los dos verificables acá:

  1. `_nearest_import(cands, c["fecha"])` — para COMPRAS se llama SIN `anio=`
     (modules/importaciones/service.py:302). Los anticipos sí pasan el año
     (línea 320). O sea que una compra se atribuye sólo por cercanía de fecha,
     con una ventana de 300 días.
  2. `_numeros_de()` expande los RANGOS: una importación cuya Nota dice
     "AC 17-35" queda indexada bajo los números 17, 18, … 35. Una compra con
     concepto 22 la tiene de candidata aunque no tenga nada que ver.

Devuelve, por cada compra de hilado del mes: a qué im_numero fue a parar, si esa
importación está recibida, y CUÁLES eran las otras candidatas con su distancia
en días. SOLO LECTURA.

    ?mes=2026-07   (default: mes en curso)
    ?prov=AC       filtrar por proveedor
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint(
    "admin_debug_cruce_compras",
    __name__,
    url_prefix="/admin/debug-cruce-compras",
)

_MES_RE = re.compile(r"^\d{4}-\d{2}$")
_PROV_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


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
    from modules.importaciones import service as svc

    mes = (request.args.get("mes") or "").strip()
    if mes and not _MES_RE.match(mes):
        return _json({"ok": False, "error": "mes invalido (YYYY-MM)"}, 400)
    if not mes:
        _h = today_ec()
        mes = f"{_h.year:04d}-{_h.month:02d}"
    prov_f = (request.args.get("prov") or "").strip().upper()
    if prov_f and not _PROV_RE.match(prov_f):
        return _json({"ok": False, "error": "prov invalido"}, 400)

    try:
        rows = svc.importaciones_con_cruce(limite=1000)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"cruce falló: {e}"}, 502)

    # Mismo índice que arma el cruce: (prov, nº) → importaciones, CON rangos.
    por_ref: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("prov") and r.get("numero") is not None:
            for n in svc._numeros_de(r):
                por_ref.setdefault(
                    (str(r["prov"]).strip().upper(), n), []).append(r)

    refs = set(por_ref.keys())
    try:
        compras = svc._buscar_compras(refs)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"no pude leer compras: {e}"}, 502)

    salida = []
    plata_en_no_recibidas = 0.0
    plata_mal_atribuida = 0.0
    for c in compras:
        prov = str(c.get("codigo_prov") or "").strip().upper()
        if prov_f and prov != prov_f:
            continue
        fecha = str(c.get("fecha") or "")[:10]
        if not fecha.startswith(mes):
            continue
        ref = int(c["ref_num"])
        cands = por_ref.get((prov, ref)) or []
        elegida = svc._nearest_import(cands, c.get("fecha"))

        def _dist(im):
            a, b = svc._to_date(im.get("fecha")), svc._to_date(c.get("fecha"))
            return abs((a - b).days) if (a and b) else None

        detalle_cands = [{
            "im": x.get("im_numero"),
            "fecha": x.get("fecha"),
            "codigo": (f"{x.get('prov')} {x.get('numero')}"
                       + (f"-{x.get('numero_hasta')}" if x.get("numero_hasta") else "")),
            "por_rango": bool(x.get("numero_hasta")),
            "recibida": bool(x.get("recibida")),
            "dias": _dist(x),
        } for x in cands]
        detalle_cands.sort(key=lambda d: (d["dias"] is None, d["dias"]))

        importe = float(c.get("importe") or 0)
        elegida_recibida = bool(elegida and elegida.get("recibida"))
        # ¿La elegida NO es la que lleva ese número "a secas"? Señal de que la
        # atribución se fue por rango o por cercanía de fecha a otra campaña.
        propia = next((x for x in cands
                       if not x.get("numero_hasta") and x.get("numero") == ref), None)
        mal = bool(elegida and propia and
                   elegida.get("im_numero") != propia.get("im_numero"))
        if elegida and not elegida_recibida:
            plata_en_no_recibidas += importe
        if mal:
            plata_mal_atribuida += importe

        salida.append({
            "compra": c.get("id_compra"),
            "fecha": fecha,
            "prov": prov,
            "concepto_ref": ref,
            "importe": round(importe, 2),
            "atribuida_a": (elegida or {}).get("im_numero"),
            "atribuida_recibida": elegida_recibida,
            "su_importacion_propia": (propia or {}).get("im_numero"),
            "atribucion_distinta_de_la_propia": mal,
            "n_candidatas": len(cands),
            "candidatas": detalle_cands[:6],
        })

    salida.sort(key=lambda s: (s["prov"], s["concepto_ref"], s["fecha"]))
    return _json({
        "ok": True,
        "mes": mes,
        "prov": prov_f or None,
        "n_compras": len(salida),
        "resumen": {
            "plata_atribuida_a_importacion_NO_recibida": round(plata_en_no_recibidas, 2),
            "plata_atribuida_a_una_importacion_QUE_NO_ES_LA_SUYA": round(plata_mal_atribuida, 2),
            "nota": ("Sólo la plata de importaciones RECIBIDAS en el mes entra al "
                     "promedio del hilado. Si una compra de algo no recibido "
                     "termina atribuida a una que sí lo está, su plata se cuela."),
        },
        "compras": salida,
    })
