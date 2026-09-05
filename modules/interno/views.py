"""`/_interno/hoja` — la oficina le saca el PDF (o la foto) al portal.

Fase 5 del plan docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md. La oficina (5002)
y el portal (5004) son el mismo código en dos procesos, y cada uno tenía SU
navegador headless prendido: dos × ~210 MB en una máquina de 4 GB para que
el portal saque un PDF cada tanto. Ahora el portal no prende navegador: le
manda el HTML a este endpoint por 127.0.0.1 y recibe los bytes. Si la
oficina no contesta, el portal cae al camino de siempre (un navegador por
archivo, que desde el 05/09 mata el árbol al terminar).

Quién puede pegarle: SÓLO 127.0.0.1 (el portal en la misma máquina) Y con
el secreto `PC_INTERNO_SECRET` en la cabecera `X-Intela-Interno`. El secreto
es una variable de MÁQUINA que el deploy genera si no está (nunca la
imprime); los dos procesos la leen del registro al arrancar (launch.py).
Sin secreto configurado, el endpoint no existe (404): así una máquina sin
él no queda con una puerta abierta "porque total es localhost".

Sólo se registra en el programa de la oficina (registro_erp): en el portal
no está, por lo mismo que no están las pantallas del ERP.
"""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request

from extensions import csrf

_LOG = logging.getLogger(__name__)
bp = Blueprint("interno", __name__, url_prefix="/_interno")

VAR_SECRETO = "PC_INTERNO_SECRET"
CABECERA = "X-Intela-Interno"
#: Un estado de cuenta grande son ~200 KB de HTML; 4 MB es de sobra.
MAX_HTML = 4 * 1024 * 1024


def secreto() -> str:
    return (os.environ.get(VAR_SECRETO) or "").strip()


def _autorizado() -> bool:
    s = secreto()
    if not s or request.remote_addr not in ("127.0.0.1", "::1"):
        return False
    return hmac.compare_digest(request.headers.get(CABECERA, ""), s)


@bp.route("/hoja", methods=["POST"])
@csrf.exempt
def hoja():
    """POST JSON {html, formato: 'pdf'|'png', fondo?, ancho?, alto?} → bytes.

    204 sin cuerpo = "por acá no salió" (el navegador de la oficina no está):
    el portal cae a su camino viejo. Nunca levanta un navegador para esto.
    """
    if not _autorizado():
        abort(404)
    datos = request.get_json(silent=True) or {}
    html = datos.get("html") or ""
    formato = (datos.get("formato") or "pdf").lower()
    if not html or len(html) > MAX_HTML or formato not in ("pdf", "png"):
        return jsonify({"error": "pedido incompleto"}), 400
    from modules._lib import navegador

    static = Path(current_app.static_folder or "static")
    if formato == "pdf":
        salida = navegador.pdf(html, static, fondo=bool(datos.get("fondo")))
        tipo = "application/pdf"
    else:
        try:
            ancho, alto = int(datos.get("ancho") or 0), int(datos.get("alto") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "medidas"}), 400
        if ancho <= 0 or alto <= 0:
            return jsonify({"error": "medidas"}), 400
        salida = navegador.png(html, static, ancho, alto)
        tipo = "image/png"
    if salida is None:
        return Response(status=204)
    return Response(salida, mimetype=tipo, headers={"Cache-Control": "no-store"})
