"""Anticipos — RETIRADO, solo redirects de compatibilidad.

TMT 2026-07-06 (dueña): "esto borrar https://programa.intela.com.ec/anticipos/
y tiene que ser esta pantalla https://programa.intela.com.ec/dolares".
El alta y la cancelación de anticipos (scintela.dolares) viven ahora en
modules/dolares (endpoints dolares.nuevo_anticipo / dolares.cancelar_anticipo)
integrados a la pantalla completa /dolares (lista, filtros, convertir-lote).

Este blueprint queda REDUCIDO a redirects para links viejos / favoritos.
No borrar el módulo del disco todavía (decisión 2026-07-06).
"""
from __future__ import annotations

from flask import Blueprint, redirect, url_for

from auth import requiere_login

bp = Blueprint("anticipos", __name__, url_prefix="/anticipos")


@bp.route("/", methods=["GET"])
@requiere_login
def lista():
    # TMT 2026-07-06 (dueña): la pantalla de anticipos es /dolares.
    return redirect(url_for("dolares.lista"), code=302)


@bp.route("/nuevo", methods=["POST"])
@requiere_login
def nuevo():
    # TMT 2026-07-06 (dueña): el alta vive en dolares.nuevo_anticipo — nadie
    # postea más acá desde la UI; redirect por si quedó algo cacheado.
    return redirect(url_for("dolares.lista"), code=302)


@bp.route("/<int:id_dolares>/cancelar", methods=["POST"])
@requiere_login
def cancelar(id_dolares: int):
    # TMT 2026-07-06 (dueña): cancelar vive en dolares.cancelar_anticipo.
    return redirect(url_for("dolares.lista"), code=302)
