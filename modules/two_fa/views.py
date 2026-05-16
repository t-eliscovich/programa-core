"""Blueprint 2FA — setup / verify / disable.

Flujo:
    1. Usuario logueado va a /2fa/setup. Si ya tiene 2FA confirmado,
       redirige al dashboard. Si no, se le genera (o reutiliza) un
       secret y se le muestra el QR para escanear con Google
       Authenticator / Authy.
    2. El usuario escanea el QR y nos manda el primer código de 6 dígitos
       por POST. Si el código es correcto, marcamos
       `totp_confirmado_en = now()`. Desde la próxima sesión, el login
       exigirá el código.
    3. En el login normal (auth.login), si `totp_confirmado_en IS NOT NULL`,
       en vez de setear `user_id` directamente, guardamos
       `pending_user_id` + `pending_next` en la sesión y redirigimos
       acá a `/2fa/verify`. Sólo después de validar el código se materializa
       la sesión completa (user_id real + last_activity).
    4. /2fa/disable es una baja expresa — requiere la contraseña
       actual para evitar que alguien que se sentó un minuto en una
       terminal abierta apague el 2FA sin dejar rastro.

Nota sobre bitácora: setup/disable se auditan automáticamente por el
hook after_request (son POST fuera de /login /logout), pero /2fa/verify
NO se audita porque el usuario real aún no está loggeado (g.user es None
durante el verify). Si queremos tener rastro de verify, hay que hacerlo
explícito — por ahora alcanza con el login row que sí queda.
"""
from __future__ import annotations

import base64
import io

import bcrypt
import qrcode
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db
from auth import _now_utc, requiere_login
from extensions import limiter
from modules.two_fa.core import generate_secret, provisioning_uri, verify

two_fa_bp = Blueprint(
    "two_fa",
    __name__,
    url_prefix="/2fa",
    template_folder="templates",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qr_png_b64(uri: str) -> str:
    """Genera un PNG del URI otpauth:// y lo devuelve como data-URL base64.

    Lo servimos inline (no como endpoint separado) para evitar tener que
    cachear el secret en memoria entre requests — el QR se regenera en
    cada GET de /setup, que es barato.
    """
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _user_by_id(user_id: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_usuario, username, password_hash, activo,
               totp_secret, totp_confirmado_en
        FROM seguridad.usuario
        WHERE id_usuario = %s
        """,
        (user_id,),
    )


# ---------------------------------------------------------------------------
# /2fa/setup — activar 2FA (usuario ya logueado)
# ---------------------------------------------------------------------------

@two_fa_bp.route("/setup", methods=["GET", "POST"])
@requiere_login
def setup():
    user_id = g.user["id_usuario"]
    row = _user_by_id(user_id)
    if not row:
        session.clear()
        return redirect(url_for("auth.login"))

    # Si ya está confirmado, no tiene sentido volver a setup — mandamos
    # a disable (o, si vienen por error, al dashboard).
    if row.get("totp_confirmado_en"):
        flash("El 2FA ya está activado en tu cuenta.", "info")
        return redirect(url_for("dashboard.index"))

    # Si todavía no hay secret, generamos uno y lo guardamos en setup-mode
    # (totp_confirmado_en = NULL). El login normal ignora secrets sin
    # confirmar, así que no hay riesgo de dejar al usuario afuera si
    # abandona el setup a medias.
    secret = row.get("totp_secret")
    if not secret:
        secret = generate_secret()
        db.execute(
            "UPDATE seguridad.usuario SET totp_secret = %s WHERE id_usuario = %s",
            (secret, user_id),
        )

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not verify(secret, code):
            flash("Código inválido. Probá de nuevo con el código que muestra la app.", "error")
            return render_template(
                "two_fa/setup.html",
                qr=_qr_png_b64(provisioning_uri(row["username"], secret)),
                secret=secret,
            ), 400

        # OK — marcamos confirmado. Desde el próximo login se pide código.
        db.execute(
            "UPDATE seguridad.usuario SET totp_confirmado_en = %s WHERE id_usuario = %s",
            (_now_utc(), user_id),
        )
        flash("2FA activado. La próxima vez que ingreses te vamos a pedir el código.", "info")
        return redirect(url_for("dashboard.index"))

    return render_template(
        "two_fa/setup.html",
        qr=_qr_png_b64(provisioning_uri(row["username"], secret)),
        secret=secret,
    )


# ---------------------------------------------------------------------------
# /2fa/verify — segundo paso del login (pending_user_id en session)
# ---------------------------------------------------------------------------

@two_fa_bp.route("/verify", methods=["GET", "POST"])
@limiter.limit(
    "5 per minute; 20 per hour",
    methods=["POST"],
    error_message="Demasiados intentos. Esperá unos minutos y volvé a probar.",
)
def verify_view():
    pending_id = session.get("pending_user_id")
    if not pending_id:
        # No hay login en curso — probablemente el usuario entró por URL directa.
        return redirect(url_for("auth.login"))

    row = _user_by_id(pending_id)
    if not row or not row["activo"] or not row.get("totp_secret") or not row.get("totp_confirmado_en"):
        session.clear()
        flash("No pudimos verificar tu cuenta. Ingresá de nuevo.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not verify(row["totp_secret"], code):
            flash("Código inválido.", "error")
            return render_template("two_fa/verify.html"), 401

        # OK — materializamos la sesión real.
        next_url = session.get("pending_next") or url_for("dashboard.index")
        session.clear()
        session["user_id"] = row["id_usuario"]
        session["last_activity"] = _now_utc().isoformat()
        session.permanent = True
        return redirect(next_url)

    return render_template("two_fa/verify.html")


# ---------------------------------------------------------------------------
# /2fa/disable — apagar 2FA (requiere password actual)
# ---------------------------------------------------------------------------

@two_fa_bp.route("/disable", methods=["GET", "POST"])
@requiere_login
def disable():
    user_id = g.user["id_usuario"]
    row = _user_by_id(user_id)
    if not row:
        session.clear()
        return redirect(url_for("auth.login"))

    if not row.get("totp_confirmado_en"):
        flash("El 2FA no está activo en tu cuenta.", "info")
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        password = request.form.get("password") or ""
        hashed = row["password_hash"]
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        if not password or not bcrypt.checkpw(password.encode("utf-8"), hashed):
            flash("Contraseña incorrecta.", "error")
            return render_template("two_fa/disable.html"), 401

        db.execute(
            """
            UPDATE seguridad.usuario
               SET totp_secret = NULL,
                   totp_confirmado_en = NULL
             WHERE id_usuario = %s
            """,
            (user_id,),
        )
        flash("2FA desactivado. Podés volver a activarlo cuando quieras.", "info")
        return redirect(url_for("dashboard.index"))

    return render_template("two_fa/disable.html")
