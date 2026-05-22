"""Google OAuth login — reemplaza el form user/password cuando está habilitado.

Habilitación:
    Si la env var `GOOGLE_CLIENT_ID` está seteada (junto con `GOOGLE_CLIENT_SECRET`
    y `APP_BASE_URL`), este módulo registra el blueprint y se vuelve la única
    forma de loguearse. Si no, el form viejo (auth.py /login POST) sigue
    funcionando para desarrollo local.

Allowlist:
    Sólo dos emails están permitidos como "Dueño". La constante está
    hardcodeada acá ADEMÁS de poder venir por env var `OAUTH_ALLOWLIST`
    (CSV) — defense in depth: si la env var se pierde, los dos emails
    siguen en el código fuente.

Comportamiento:
    /auth/google/login    → redirige al consent screen de Google
    /auth/google/callback → recibe el token, valida email contra allowlist,
                            upsertea fila en seguridad.usuario con rol Dueño,
                            setea session["user_id"] y redirige al dashboard.

    Si el email NO está en la allowlist devuelve 403 con un mensaje claro
    (sin filtrar info sobre quién tiene acceso).

Sesión:
    Usa el MISMO mecanismo que el login viejo:
        session["user_id"]       = id_usuario
        session["last_activity"] = ahora ISO
        session.permanent        = True
    De modo que `load_logged_in_user` y todos los decoradores
    (requiere_login, requiere_permiso, etc.) siguen funcionando sin cambios.

Notas:
    - Usamos Authlib porque maneja state/PKCE/discovery automáticamente.
    - El callback hace UPSERT para que el primer login funcione sin seed
      manual. Idempotente en logins posteriores.
    - Si el rol "Dueño" no existe en la DB el callback devuelve 500
      explicando exactamente qué falta. Esto fuerza a correr las migraciones
      antes que la app pueda servir.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

import bcrypt
from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    session,
    url_for,
)

import db

_log = logging.getLogger("programa_core.oauth")

UTC = timezone.utc  # noqa: UP017 — auth.py mantiene esto para compat py3.10

auth_google_bp = Blueprint("auth_google", __name__, url_prefix="/auth/google")

# Fallback hardcodeado: los dos dueños. Si la env var OAUTH_ALLOWLIST se
# pierde por error de deploy, los dos emails siguen acá.
_DEFAULT_ALLOWLIST = frozenset(
    {
        "teliscovich@gmail.com",
        "feliscovich@gmail.com",
    }
)


def _allowlist() -> frozenset[str]:
    """Devuelve el conjunto de emails permitidos.

    Si la env var OAUTH_ALLOWLIST está seteada (CSV), la usa.
    Si no, cae al default hardcodeado. UNIÓN con el default para que
    una env var mal escrita no quite acceso a los dueños originales.
    """
    raw = os.environ.get("OAUTH_ALLOWLIST", "")
    if not raw:
        return _DEFAULT_ALLOWLIST
    extras = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return frozenset(_DEFAULT_ALLOWLIST | extras)


# Authlib OAuth client — se inicializa una sola vez en init_oauth(app).
_oauth: OAuth | None = None


def init_oauth(app) -> None:
    """Llamada desde create_app() después de cargar la config.

    No-op si GOOGLE_CLIENT_ID no está seteado — útil para dev local
    donde no querés tener OAuth configurado.
    """
    global _oauth
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        _log.info("Google OAuth desactivado (faltan env vars GOOGLE_CLIENT_ID/SECRET)")
        app.config["GOOGLE_OAUTH_ENABLED"] = False
        return

    _oauth = OAuth(app)
    _oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    app.config["GOOGLE_OAUTH_ENABLED"] = True
    _log.info("Google OAuth activado (allowlist tiene %d emails)", len(_allowlist()))


def google_oauth_enabled() -> bool:
    """Helper para templates: {% if google_oauth_enabled() %} ... {% endif %}"""
    return bool(current_app.config.get("GOOGLE_OAUTH_ENABLED"))


@auth_google_bp.route("/login")
def login():
    """Inicia el flow OAuth — redirige al consent screen de Google."""
    if not google_oauth_enabled():
        abort(404)
    # APP_BASE_URL debe estar definida en prod (https://programa.intela.com.ec).
    # En dev usamos url_for que respeta el host del request.
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if base:
        redirect_uri = f"{base}/auth/google/callback"
    else:
        redirect_uri = url_for("auth_google.callback", _external=True)
    return _oauth.google.authorize_redirect(redirect_uri)


@auth_google_bp.route("/callback")
def callback():
    """Recibe el code de Google, valida y arranca la sesión."""
    if not google_oauth_enabled():
        abort(404)

    try:
        token = _oauth.google.authorize_access_token()
    except Exception as e:
        _log.warning("OAuth callback falló intercambio de token: %s", e)
        flash("No se pudo completar el inicio de sesión con Google.", "error")
        return redirect(url_for("auth.login"))

    # `userinfo` viene parseado del id_token por Authlib.
    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    email_verified = bool(userinfo.get("email_verified"))
    name = userinfo.get("name") or email

    if not email or not email_verified:
        _log.warning("OAuth callback: email faltante o no verificado: %r", userinfo)
        flash("Tu cuenta de Google no tiene un email verificado.", "error")
        return redirect(url_for("auth.login"))

    allowlist = _allowlist()
    if email not in allowlist:
        _log.warning("OAuth callback: email %s NO está en allowlist", email)
        # 403 explícito sin filtrar quién está en la allowlist.
        return (
            "<html><body style='font-family:sans-serif;padding:40px;max-width:480px;margin:auto'>"
            "<h2>Acceso denegado</h2>"
            f"<p>La cuenta <b>{email}</b> no tiene acceso a Programa Core.</p>"
            "<p>Si pensás que esto es un error, contactá a un administrador.</p>"
            "<p><a href='/auth/google/login'>Probar con otra cuenta de Google</a></p>"
            "</body></html>",
            403,
        )

    # Upsertear el usuario en seguridad.usuario con rol Dueño.
    try:
        id_usuario = _upsert_owner(email=email, display_name=name)
    except _DuenoRoleMissing:
        _log.error(
            "Rol 'Accionista' (ni el legacy 'Dueño') existe en seguridad.rol — la DB no está seedeada."
        )
        return (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>Error de configuración</h2>"
            "<p>El rol <code>Accionista</code> no existe en la tabla <code>seguridad.rol</code>. "
            "Corré las migraciones / seeds antes de ingresar.</p>"
            "</body></html>",
            500,
        )

    # Login OK — armamos sesión del mismo modo que el login viejo.
    session.clear()
    session["user_id"] = id_usuario
    session["last_activity"] = datetime.now(UTC).isoformat()
    session.permanent = True

    _log.info("OAuth login OK: %s (id_usuario=%s)", email, id_usuario)
    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


class _DuenoRoleMissing(Exception):
    """El rol Dueño no existe — la DB no está seedeada."""


def _email_to_role_map() -> dict[str, str]:
    """Mapeo email→nombre_rol leído de env var OAUTH_ROLE_MAP.

    Formato: 'email1=Rol1,email2=Rol2,...' (case-insensitive en email).
    Ej.: 'alex@gmail.com=Alex,andres@gmail.com=Administrador'.

    Si la env var no está seteada o el email no figura, el caller debe
    fallback al rol default (Accionista). TMT 2026-05-21.
    """
    raw = os.environ.get("OAUTH_ROLE_MAP", "")
    if not raw:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        email, rol = entry.split("=", 1)
        email = email.strip().lower()
        rol = rol.strip()
        if email and rol:
            out[email] = rol
    return out


def _upsert_owner(*, email: str, display_name: str) -> int:
    """Crea o actualiza el usuario y devuelve su id_usuario.

    Si el email está mapeado en OAUTH_ROLE_MAP a un rol específico,
    usa ese rol. Sino, default a Accionista (compat con flujo viejo
    de los dueños).

    Idempotente: si el usuario ya existe, asegura activo=TRUE y el
    id_rol correcto.

    Si el rol no existe en seguridad.rol, levanta _DuenoRoleMissing
    para que el caller devuelva un 500 con mensaje claro.
    """
    # TMT 2026-05-21 dueña: rol asignado por email. Alex va a 'Alex'
    # (ver pantallas operativas pero NO Informes). Andres va a
    # 'Administrador'. Si no hay mapping, default a Accionista (compat).
    role_map = _email_to_role_map()
    nombre_rol_deseado = role_map.get(email.lower())

    role = None
    if nombre_rol_deseado:
        role = db.fetch_one(
            "SELECT id_rol FROM seguridad.rol WHERE lower(nombre_rol) = %s",
            (nombre_rol_deseado.lower(),),
        )
        if not role:
            _log.warning(
                "OAUTH_ROLE_MAP referencia rol %r que no existe en seguridad.rol — "
                "cayendo a Accionista para %s",
                nombre_rol_deseado,
                email,
            )

    if not role:
        # TMT 2026-05-19 v8 — "Dueño" renombrado a "Accionista" (pedido dueña).
        # Buscamos primero el nombre nuevo y caemos a "dueño" (legacy) si la
        # migración 0035 todavía no se corrió.
        role = db.fetch_one("SELECT id_rol FROM seguridad.rol WHERE lower(nombre_rol) = 'accionista'")
    if not role:
        role = db.fetch_one("SELECT id_rol FROM seguridad.rol WHERE lower(nombre_rol) = 'dueño'")
    if not role:
        # Tolerancia a sin-tilde por si el seed lo guarda como "Dueno".
        role = db.fetch_one(
            "SELECT id_rol FROM seguridad.rol "
            "WHERE lower(nombre_rol) IN ('dueno', 'owner', 'admin', 'administrador')"
        )
    if not role:
        raise _DuenoRoleMissing()
    id_rol = role["id_rol"]

    # TMT 2026-05-22 dueña: resolver primero por columna `email` (consolidación
    # opción A — un usuario por persona con username corto + email separado).
    # Fallback al esquema viejo (username == email) por compat con instalaciones
    # sin la migración 0045.
    existing = db.fetch_one(
        "SELECT id_usuario FROM seguridad.usuario WHERE lower(email) = %s",
        (email,),
    )
    if not existing:
        existing = db.fetch_one(
            "SELECT id_usuario FROM seguridad.usuario WHERE lower(username) = %s",
            (email,),
        )
    if existing:
        # Aseguramos que esté activo, con rol correcto y el email seteado.
        db.execute(
            """
            UPDATE seguridad.usuario
               SET activo = TRUE,
                   id_rol = %s,
                   email  = COALESCE(email, %s)
             WHERE id_usuario = %s
            """,
            (id_rol, email, existing["id_usuario"]),
        )
        return existing["id_usuario"]

    # Insert nuevo. Password hash random — nunca se usa para login con OAuth.
    random_pw = secrets.token_urlsafe(32).encode("utf-8")
    pw_hash = bcrypt.hashpw(random_pw, bcrypt.gensalt()).decode("utf-8")

    # No incluimos columnas que la migración 0008 agrega (totp_*,
    # password_debe_cambiar). Asumimos defaults razonables (NULL / FALSE)
    # en el schema. Si la tabla tiene NOT NULL en alguna, ajustar al
    # restaurar el dump.
    # TMT 2026-05-22 — username = prefijo antes de @ (federico, andres),
    # email = mail completo. Si el username ya existe (race), agregamos
    # un sufijo numérico.
    username = email.split("@")[0].lower() or email.lower()
    sufijo = 0
    while db.fetch_one(
        "SELECT 1 FROM seguridad.usuario WHERE lower(username) = %s",
        (username if sufijo == 0 else f"{username}{sufijo}",),
    ):
        sufijo += 1
        if sufijo > 99:
            username = email.lower()  # fallback final
            break
    if sufijo > 0:
        username = f"{username}{sufijo}"
    row = db.execute_returning(
        """
        INSERT INTO seguridad.usuario (username, email, password_hash, id_rol, activo)
        VALUES (%s, %s, %s, %s, TRUE)
        RETURNING id_usuario
        """,
        (username, email.lower(), pw_hash, id_rol),
    )
    if not row:
        raise RuntimeError(f"INSERT seguridad.usuario no devolvió id (email={email})")
    return row["id_usuario"]
