"""Pantalla NOVEDADES — todo lo que el programa hizo o vio, en una sola lista.

TMT 2026-07-30 (dueña): *"una pantalla novedades nueva"* + *"que la pantalla no
diga convertir a compra etc: el punto es avisar"*. Por eso acá NO hay switches,
ni topes, ni botones de operación: se lee y, si algo hay que hacer, se va a la
pantalla de ese tema por el link del aviso. La pantalla del automático de
importaciones sigue existiendo aparte, con sus controles.
"""
from flask import Blueprint, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso

from . import queries

avisos_bp = Blueprint("avisos", __name__, template_folder="templates")


@avisos_bp.route("/novedades")
@requiere_login
@requiere_permiso("compras.ver")
def lista():
    fuente = (request.args.get("fuente") or "").strip() or None
    nivel = (request.args.get("nivel") or "").strip() or None
    todos = (request.args.get("todos") or "1").strip() not in ("0", "no", "")
    items = queries.listar(
        solo_no_leidos=not todos, limite=200, fuente=fuente, nivel=nivel,
    )
    return render_template(
        "avisos/lista.html", items=items, fuente=fuente, nivel=nivel,
        todos=todos, fuentes=queries.FUENTES, n_no_leidos=queries.n_no_leidos(),
    )


@avisos_bp.route("/novedades/leidos", methods=["POST"])
@requiere_login
@requiere_permiso("compras.ver")
def leidos():
    """Marca todo como leído.

    La campanita lo llama por fetch al abrirse (`ajax=1`, dueña 30/07: "una vez
    que lo abro se marque leído solo") y no espera respuesta → 204.
    """
    queries.marcar_leidos()
    if request.form.get("ajax") == "1":
        return ("", 204)
    return redirect(request.referrer or url_for("avisos.lista"))
