"""/admin/debug-grupos-partidas — qué importaciones son MITADES de una misma.

TMT 2026-07-31. Medido contra el dBase: AC 25 = 48.988 kg reales / 24.494 en
PC, AC 19 = 22.354 / 10.990, MH 66 = 46.860 / 23.430, AI 11 idem. Asinfo parte
una factura en dos documentos `factura_proveedor` y cada uno se lleva la mitad
de los kilos.

Este endpoint NO calcula nada nuevo: corre la MISMA función que va a usar el
balance (`adjuntar_grupo_partidas`) y muestra, importación por importación, la
nota cruda, la nota base normalizada, el nº de partida detectado y en qué grupo
quedó — o por qué se descartó.

Existe porque la regla de agrupación se escribió sobre una hipótesis y hay que
mirarla contra los datos reales antes de dejar que mueva la utilidad. En
particular: **el descalificador "algún miembro trae rango"**. En julio las dos
mitades de AC 25 son IM-0000548 e IM-0000549, y las DOS traen el rango
"AC 25-26" — si el descalificador está de más, esos 24.494 kg no se recuperan.

SOLO LECTURA.

    ?prov=AC        filtrar por código del programa
    ?solo=partidas  sólo las filas con sufijo ---N
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_grupos_partidas",
    __name__,
    url_prefix="/admin/debug-grupos-partidas",
)

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
    from concepto_parser import parse_nota_importacion
    from modules.asinfo import service as asvc
    from modules.importaciones import service as svc

    prov_f = (request.args.get("prov") or "").strip().upper()
    if prov_f and not _PROV_RE.match(prov_f):
        return _json({"ok": False, "error": "prov invalido"}, 400)
    solo_partidas = (request.args.get("solo") or "").strip().lower() == "partidas"

    try:
        rows = [dict(r) for r in asvc.importaciones_asinfo(limite=1000)]
        kg_map = asvc.importaciones_kg(limite=1000)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"Asinfo no contestó: {e}"}, 502)

    for r in rows:
        r["kg"] = kg_map.get(str(r.get("im_numero") or "").strip())
        code = parse_nota_importacion(r.get("nota"))
        r["prov"] = code.get("prov")
        r["numero"] = code.get("numero")
        r["numero_hasta"] = code.get("numero_hasta")
    svc.adjuntar_grupo_partidas(rows)

    salida = []
    for r in rows:
        if prov_f and str(r.get("prov") or "").upper() != prov_f:
            continue
        base, suf = svc._partida_de(r.get("nota"))
        if solo_partidas and suf is None:
            continue
        salida.append({
            "im": r.get("im_numero"),
            "fecha": r.get("fecha"),
            "fecha_recepcion": r.get("fecha_recepcion"),
            "recibida": bool(r.get("recibida")),
            "prov_cod_asinfo": r.get("prov_cod_asinfo"),
            "codigo": (f"{r.get('prov')} {r.get('numero')}"
                       + (f"-{r.get('numero_hasta')}" if r.get("numero_hasta") else "")),
            "nota_cruda": r.get("nota"),
            "nota_base": base,
            "partida_n": suf,
            "kg": r.get("kg"),
            "grupo_id": r.get("grupo_id"),
            "grupo_kg": r.get("grupo_kg"),
            "grupo_ims": r.get("grupo_ims"),
            "grupo_completo": r.get("grupo_completo"),
            "grupo_aviso": r.get("grupo_aviso"),
        })
    salida.sort(key=lambda s: (str(s["fecha"] or ""), str(s["im"] or "")), reverse=True)

    # Resumen: qué grupos de MÁS DE UNO se armaron, y cuáles se descartaron.
    armados: dict[str, dict] = {}
    descartados: dict[str, dict] = {}
    for r in rows:
        gid = str(r.get("grupo_id") or "")
        if r.get("grupo_aviso"):
            d = descartados.setdefault(gid, {
                "motivo": r.get("grupo_aviso"), "ims": [], "kg_por_fila": [],
                "codigo": f"{r.get('prov')} {r.get('numero')}",
            })
            d["ims"].append(r.get("im_numero"))
            d["kg_por_fila"].append(r.get("kg"))
        elif len(r.get("grupo_ims") or []) > 1:
            armados[gid] = {
                "ims": r.get("grupo_ims"), "grupo_kg": r.get("grupo_kg"),
                "codigo": f"{r.get('prov')} {r.get('numero')}",
                "fecha": r.get("fecha"), "recibida": bool(r.get("recibida")),
            }

    return _json({
        "ok": True,
        "prov": prov_f or None,
        "n_importaciones": len(salida),
        "regla": ("mismo prov_cod_asinfo + misma nota base (numero de factura del "
                  "proveedor, sin parentesis ni ordinal) + MISMO codigo del programa "
                  "(prov + n + n_hasta) + fechas dentro de 120 dias. Descalifica: mas "
                  "de 3 miembros, falta el kg de alguno, o se recibieron en meses "
                  "distintos."),
        "grupos_armados": armados,
        "grupos_descartados": descartados,
        "importaciones": salida,
    })
