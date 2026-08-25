"""Las pantallas del portal del cliente.

Un solo camino, sin ramas (`PLAN_PORTAL_CLIENTE_2026_08_24.md`):

    /ingresar        código + clave — la primera vez, código + RUC
    /elegir-clave    elige su clave y confirma su correo
    /                lo que ve adentro
    /salir

⚠ La sesión del cliente vive en una llave PROPIA (`portal_cliente`), separada
de la de los empleados (`user_id`). No se mezclan a propósito: en este proceso
el login de empleados ni siquiera existe.
"""
from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup

from extensions import limiter
from modules.portal import acceso

portal_bp = Blueprint("portal", __name__, url_prefix="",
                      template_folder="templates")

#: La llave de la sesión del cliente. Distinta de `user_id`, que es la de los
#: empleados: que un portal público comparta llave con el ERP es justo lo que
#: este proceso vino a evitar.
LLAVE = "portal_cliente"


@portal_bp.app_context_processor
def _comunes():
    from flask_wtf.csrf import generate_csrf

    return {
        "hoy": date.today().strftime("%d/%m/%Y"),
        "csrf_token_input": Markup(
            f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">'),
    }


def cliente_actual() -> str:
    """El código del cliente logueado, o vacío."""
    return (session.get(LLAVE) or "").strip().upper()


def _pedir_entrar():
    return redirect(url_for("portal.ingresar"))


def _de_donde() -> tuple[str, str]:
    """IP y navegador, para la bitácora."""
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")
            .split(",")[0].strip(),
            request.headers.get("User-Agent", ""))


# ---------------------------------------------------------------------------
# Entrar
# ---------------------------------------------------------------------------


@portal_bp.route("/ingresar", methods=["GET", "POST"])
@limiter.limit("20 per minute; 100 per hour", methods=["POST"])
def ingresar():
    if request.method == "GET":
        if cliente_actual():
            return redirect(url_for("portal.inicio"))
        return render_template("portal/ingreso.html", codigo="")

    codigo = (request.form.get("codigo") or "").strip()
    secreto = request.form.get("secreto") or ""
    ip, navegador = _de_donde()
    r = acceso.entrar(codigo, secreto, ip=ip, navegador=navegador)

    if not r["ok"]:
        flash(r["mensaje"], "error")
        # El código se devuelve para no hacerlo escribir de nuevo; la clave no.
        return render_template("portal/ingreso.html",
                               codigo=acceso.normalizar_codigo(codigo)), 401

    # Sesión nueva y limpia en cada ingreso: si quedaba algo de antes, se va.
    session.clear()
    session[LLAVE] = r["codigo_cli"]
    session.permanent = True

    if r["elegir_clave"]:
        return redirect(url_for("portal.elegir_clave"))
    return redirect(url_for("portal.inicio"))


@portal_bp.route("/elegir-clave", methods=["GET", "POST"])
def elegir_clave():
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    fic = acceso.cliente(cod) or {}
    acc = acceso.acceso(cod) or {}
    mail_previo = (acc.get("mail") or "").strip()

    if request.method == "GET":
        return render_template("portal/elegir_clave.html",
                               nombre=fic.get("nombre") or cod,
                               mail=mail_previo)

    clave = request.form.get("clave") or ""
    repetir = request.form.get("repetir") or ""
    mail = request.form.get("mail") or ""
    mail2 = request.form.get("mail2") or ""

    def _volver(problema):
        flash(problema, "error")
        return render_template("portal/elegir_clave.html",
                               nombre=fic.get("nombre") or cod,
                               mail=mail or mail_previo), 400

    if clave != repetir:
        return _volver("Las dos claves no son iguales.")

    # El correo se comprueba ANTES de guardar la clave: si algo está mal, el
    # cliente vuelve a una pantalla que se comporta igual que la primera vez.
    ok, msg = acceso.mail_aceptable(mail, mail2)
    if not ok:
        return _volver(msg)

    ok, msg = acceso.guardar_clave(cod, clave)
    if not ok:
        return _volver(msg)

    acceso.guardar_mail(cod, mail, mail_previo)
    flash(msg, "ok")
    return redirect(url_for("portal.inicio"))


# ---------------------------------------------------------------------------
# Olvidé mi clave
# ---------------------------------------------------------------------------


@portal_bp.route("/olvide-la-clave", methods=["GET", "POST"])
@limiter.limit("6 per minute; 30 per hour", methods=["POST"])
def olvide_la_clave():
    if request.method == "GET":
        return render_template("portal/olvide.html", codigo="")

    codigo = (request.form.get("codigo") or "").strip()
    seis, mail = acceso.pedir_codigo(codigo)
    if seis:
        _mandar_el_codigo(codigo, seis, mail)

    # ⭐ La MISMA respuesta exista o no el cliente, y tenga o no correo. Si
    # dijera "ese cliente no tiene correo", esta pantalla sería un buscador de
    # códigos de cliente reales — y el código son sólo 3 letras.
    flash(acceso.MANDADO, "ok")
    return render_template("portal/codigo.html",
                           codigo=acceso.normalizar_codigo(codigo),
                           minutos=acceso.MINUTOS_CODIGO)


def _mandar_el_codigo(codigo: str, seis: str, mail: str) -> None:
    """Manda el código por correo. Nunca rompe la pantalla.

    Si el mail no sale, el cliente ve el mismo mensaje de siempre y puede
    pedir otro. Decírselo sería decirle que ese código existe.
    """
    try:
        from modules._lib import mailer

        texto = (f"Su código para entrar al portal de Intela es: {seis}\n\n"
                 f"Vale por {acceso.MINUTOS_CODIGO} minutos. "
                 f"Si no lo pidió usted, ignore este mensaje.")
        mailer.enviar("Intela · su código para entrar", texto, [mail])
    except Exception:  # noqa: BLE001 -- el mail nunca tumba la pantalla
        pass


@portal_bp.route("/codigo", methods=["POST"])
@limiter.limit("10 per minute; 40 per hour")
def codigo():
    """El código de 6 números que llegó por correo."""
    cod = acceso.normalizar_codigo(request.form.get("codigo") or "")
    seis = request.form.get("seis") or ""
    ip, navegador = _de_donde()

    if not acceso.usar_codigo(cod, seis):
        acceso.anotar(cod, "codigo_malo", "codigo", ip, navegador)
        flash("Ese código no sirve. Puede estar vencido o ya usado.", "error")
        return render_template("portal/codigo.html", codigo=cod,
                               minutos=acceso.MINUTOS_CODIGO), 401

    acceso.anotar(cod, "ok", "codigo", ip, navegador)
    session.clear()
    session[LLAVE] = cod
    session.permanent = True
    # Entró con el correo, no con la clave: lo que sigue es elegir una nueva.
    flash("Elegí una clave nueva.", "ok")
    return redirect(url_for("portal.elegir_clave"))


@portal_bp.route("/salir", methods=["GET", "POST"])
def salir():
    session.clear()
    return _pedir_entrar()


# ---------------------------------------------------------------------------
# Adentro
# ---------------------------------------------------------------------------


@portal_bp.route("/", methods=["GET"])
def inicio():
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    acc = acceso.acceso(cod)
    # Le cortaron el acceso mientras estaba adentro: se va en el próximo click.
    if acc and not acc.get("activo", True):
        session.clear()
        flash(acceso.CORTADO, "error")
        return _pedir_entrar()
    # El cliente se borró o le cambiaron el código: no se inventa una pantalla.
    fic = acceso.cliente(cod)
    if not fic:
        session.clear()
        return _pedir_entrar()

    # Todavía no eligió clave: no puede quedarse a mitad de camino.
    if acc is not None and not acc.get("clave_hash"):
        return redirect(url_for("portal.elegir_clave"))

    return render_template("portal/inicio.html",
                           nombre=fic.get("nombre") or cod, codigo=cod)
