"""Lista de precios (réplica de PRECIOS.DBF) — Datos base.

Matriz de clases de color (filas) x 12 tipos de tela (columnas), valor =
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
    """Ver la lista de precios. Sólo login — la ven todos los roles.

    Muestra SÓLO la matriz base (precio USD/kg por clase de color x tela),
    igual que el dBase. Por defecto es de sólo lectura; el botón "Editar"
    (visible sólo con permiso 'precios.editar') habilita la edición en la
    misma pantalla.
    """
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


@precios_bp.route("/precios/subir-porcentaje", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def subir_porcentaje():
    """Sube TODOS los precios de la matriz un % (la forma normal de actualizar).

    Gated 'precios.editar'. El % se parsea con el parser EU/es-EC (coma
    decimal). Un % <= -100 dejaría precios en cero/negativos → se rechaza.
    """
    pct_raw = request.form.get("pct")
    pct = parse_monto(pct_raw)
    if pct is None:
        flash(f"No entendí el porcentaje «{pct_raw}». Usá formato 5 ó 5,5.", "error")
        return redirect(url_for("precios.lista"))
    if float(pct) <= -100:
        flash("El porcentaje no puede dejar los precios en cero o negativos.", "error")
        return redirect(url_for("precios.lista"))

    usuario = (g.user or {}).get("username", "web")
    try:
        queries.subir_porcentaje(float(pct), usuario)
        signo = "+" if float(pct) >= 0 else ""
        flash(f"Todos los precios actualizados {signo}{pct}%.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude subir los precios", e)
    return redirect(url_for("precios.lista"))


@precios_bp.route("/precios/guardar", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def guardar():
    """Guarda TODA la matriz de una vez (modo edición → botón "Guardar").

    Recibe un valor por celda con nombre `p_<clase>_<columna>`. Sólo escribe
    las celdas cuyo valor cambió respecto de la matriz actual. Gated
    'precios.editar'.
    """
    usuario = (g.user or {}).get("username", "web")
    try:
        actuales = queries.matriz()
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude leer la lista de precios", e)
        return redirect(url_for("precios.lista"))

    cambios = 0
    errores = []
    for fila in actuales:
        clase = int(fila["clase"])
        for col in (c for c, _ in queries.TELAS):
            campo = f"p_{clase}_{col}"
            if campo not in request.form:
                continue
            raw = (request.form.get(campo) or "").strip()
            nuevo = parse_monto(raw)
            if raw and nuevo is None:
                errores.append(f"«{raw}»")
                continue
            actual = fila.get(col)
            # Comparar como float para no re-escribir por formato.
            act_f = float(actual) if actual is not None else None
            nue_f = float(nuevo) if nuevo is not None else None
            if act_f == nue_f:
                continue
            try:
                queries.actualizar_precio(clase, col, nuevo, usuario)
                cambios += 1
            except Exception:  # noqa: BLE001
                errores.append(f"{clase}/{col}")

    if errores:
        flash("No entendí algunos precios: " + ", ".join(errores[:8]), "error")
    if cambios:
        flash(f"{cambios} precio(s) actualizado(s).", "ok")
    elif not errores:
        flash("Sin cambios.", "ok")
    return redirect(url_for("precios.lista"))
