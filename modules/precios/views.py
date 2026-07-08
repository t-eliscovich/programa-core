"""Lista de precios (réplica de PRECIOS.DBF) — Datos base.

Matriz 5 clases de color (filas) x 12 tipos de tela (columnas), valor =
precio USD/kg. TODOS pueden VER (sólo requiere login). EDITAR sólo lo pueden
los roles con permiso 'precios.editar' — que los roles wildcard '*'
(Accionista = Federico/Tamara, Administrador = Andres) satisfacen
automáticamente, sin tocar roles.py ni migrar seguridad.
"""
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from parsers import parse_monto

from . import queries

precios_bp = Blueprint("precios", __name__, template_folder="templates")


@precios_bp.route("/precios")
@requiere_login
def lista():
    """Ver la lista de precios. Sólo login — la ven todos los roles."""
    try:
        filas = queries.matriz()
        error = None
    except Exception as e:  # noqa: BLE001
        filas, error = [], str(e)
    return render_template(
        "precios/lista.html",
        filas=filas,
        telas=queries.TELAS,
        error=error,
    )


@precios_bp.route("/precios/actualizar", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def actualizar():
    """Actualiza una celda de precio (clase, tela). Gated 'precios.editar'."""
    from parsers import parse_int

    clase = parse_int(request.form.get("clase"))
    columna = (request.form.get("columna") or "").strip().lower()
    valor_raw = request.form.get("valor")

    if clase is None or columna not in queries.COLUMNAS_TELA:
        flash("No pude identificar la celda a actualizar.", "error")
        return redirect(url_for("precios.lista"))

    valor = parse_monto(valor_raw)
    if valor_raw and valor_raw.strip() and valor is None:
        flash(f"No entendí el precio «{valor_raw}». Usá formato 9,12.", "error")
        return redirect(url_for("precios.lista"))

    usuario = (g.user or {}).get("username", "web")
    try:
        queries.actualizar_precio(clase, columna, valor, usuario)
        flash("Precio actualizado.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude actualizar el precio", e)
    return redirect(url_for("precios.lista"))
