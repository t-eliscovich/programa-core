"""Pantalla NOVEDADES — todo lo que el programa hizo o vio, en una sola lista.

TMT 2026-07-30 (dueña): *"una pantalla novedades nueva"* + *"que la pantalla no
diga convertir a compra etc: el punto es avisar"*. Por eso acá NO hay switches,
ni topes, ni botones de operación: se lee y, si algo hay que hacer, se va a la
pantalla de ese tema por el link del aviso. La pantalla del automático de
importaciones sigue existiendo aparte, con sus controles.
"""
from flask import Blueprint, g, redirect, render_template, request, url_for

from auth import requiere_login

from . import queries, visibilidad

avisos_bp = Blueprint("avisos", __name__, template_folder="templates")


# TMT 2026-08-19 (dueña): *"que vean de cualquier notificación que sea páginas
# que ellos están habilitados"*. Las tres rutas dejaron de pedir `compras.ver`
# —que dejaba afuera a INT desde el 05/08— y el control pasó a ser POR AVISO:
# se ve el que lleva a una pantalla que la persona puede abrir (visibilidad.py).
# Sin avisos visibles, la pantalla se ve vacía; no hay nada que esconder acá.
@avisos_bp.route("/novedades")
@requiere_login
def lista():
    fuente = (request.args.get("fuente") or "").strip() or None
    nivel = (request.args.get("nivel") or "").strip() or None
    todos = (request.args.get("todos") or "1").strip() not in ("0", "no", "")
    archivados = (request.args.get("archivados") or "").strip() in ("1", "si", "sí")
    items = queries.listar(
        solo_no_leidos=not todos, limite=200, fuente=fuente, nivel=nivel,
        archivados=archivados,
    )
    return render_template(
        "avisos/lista.html", items=items, fuente=fuente, nivel=nivel,
        todos=todos,
        fuentes=visibilidad.fuentes_visibles(queries.FUENTES, items, fuente),
        n_no_leidos=queries.n_no_leidos(),
        archivados=archivados,
        # El deploy no corre migraciones: hasta que se aplique la 0145 la
        # pantalla se ve exactamente como antes, sin × y sin chip.
        hay_archivo=queries._tiene_archivado(),
    )


@avisos_bp.route("/novedades/<int:id_aviso>/archivar", methods=["POST"])
@requiere_login
def archivar(id_aviso: int):
    """Saca un aviso de la lista (o lo devuelve, con `deshacer=1`).

    Para los que quedaron viejos: el aviso decía algo cierto cuando se emitió y
    dejó de serlo cuando se arregló el problema. Ver queries.archivar.
    """
    # Archivar es sacar el aviso de la lista de TODOS: sólo puede hacerlo
    # quien podría abrirlo (mismo criterio que para verlo).
    a = queries.obtener(id_aviso)
    if a and not visibilidad.puede_ver(a.get("fuente"), a.get("url")):
        return render_template("404.html"), 404
    deshacer = (request.form.get("deshacer") or "") == "1"
    queries.archivar(id_aviso, (g.user or {}).get("username", "web"),
                     deshacer=deshacer)
    return redirect(request.referrer or url_for("avisos.lista"))


@avisos_bp.route("/novedades/leidos", methods=["POST"])
@requiere_login
def leidos():
    """Marca todo como leído.

    La campanita lo llama por fetch al abrirse (`ajax=1`, dueña 30/07: "una vez
    que lo abro se marque leído solo") y no espera respuesta → 204.
    """
    queries.marcar_leidos()
    if request.form.get("ajax") == "1":
        return ("", 204)
    return redirect(request.referrer or url_for("avisos.lista"))
