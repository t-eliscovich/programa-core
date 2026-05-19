"""Login, sessions, and permission decorators.

Tables used:
    seguridad.usuario  (username, password_hash, id_rol, activo)
    seguridad.rol      (id_rol, nombre_rol)
    seguridad.permiso  (id_rol, nombre_opcion)

Session timeouts (sliding, per rol):
    Roles transaccionales (Contabilidad, Compras, Cobranzas, Ventas)
    expiran más rápido que roles administrativos o de lectura. Ver
    `SESSION_TIMEOUT_BY_ROLE` más abajo y `_enforce_session_timeout`
    en load_logged_in_user.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
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
from extensions import limiter

# datetime.UTC es py3.11+. Aliasamos timezone.utc como UTC para compat py3.10
# (Ubuntu 22.04 LTS) sin romper el resto del código que usa UTC directo.
UTC = timezone.utc  # noqa: UP017

auth_bp = Blueprint("auth", __name__, template_folder="modules/auth/templates")


# ---------------------------------------------------------------------------
# Session timeout por rol — sliding window.
# ---------------------------------------------------------------------------
# Cada sesión expira cuando `now - session["last_activity"] > timeout`, donde
# `timeout` depende del rol. El contador se REINICIA en cada request (sliding).
# Esto balancea UX (el gerente no se desloguea en medio de leer un informe)
# con seguridad (una terminal de caja olvidada no se queda abierta toda la
# noche).
#
# Valores elegidos:
#   - Roles transaccionales → 4h   (mueven plata; queremos re-auth temprano)
#   - Roles administrativos → 8h   (jornada completa normal)
#   - Roles de piso         → 12h  (bodega/QC, turnos largos, bajo riesgo)
#   - Roles de lectura      → 24h  (sin writes, molestar menos)
# Si aparece un rol nuevo sin entrada, cae al default (4h) — seguro por
# omisión.
SESSION_TIMEOUT_BY_ROLE: dict[str, timedelta] = {
    # TMT 2026-05-19 v8 — "Dueño" renombrado a "Accionista" (pedido dueña).
    # Dejamos los dos para compatibilidad transitoria mientras se corre la
    # migración 0035; cualquiera de los dos resuelve al mismo timeout.
    "Accionista":    timedelta(hours=8),
    "Dueño":         timedelta(hours=8),
    "Administrador": timedelta(hours=8),
    "Gerente":       timedelta(hours=8),
    "Contabilidad":  timedelta(hours=4),
    "Compras":       timedelta(hours=4),
    "Cobranzas":     timedelta(hours=4),
    "Ventas":        timedelta(hours=4),
    "Bodega":        timedelta(hours=12),
    "QC":            timedelta(hours=12),
    "Lectura":       timedelta(hours=24),
}
SESSION_TIMEOUT_DEFAULT = timedelta(hours=4)


def timeout_for_role(nombre_rol: str | None) -> timedelta:
    """Devuelve el timeout aplicable al rol, o el default si no hay match."""
    if not nombre_rol:
        return SESSION_TIMEOUT_DEFAULT
    return SESSION_TIMEOUT_BY_ROLE.get(nombre_rol, SESSION_TIMEOUT_DEFAULT)


def _now_utc() -> datetime:
    """UTC aware now. Separado para que los tests puedan monkeypatchearlo."""
    return datetime.now(UTC)


def _parse_last_activity(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    # Si es naive (vinieron de una versión anterior sin tz), asumir UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def load_logged_in_user() -> None:
    """before_request handler: populate g.user and g.permisos.

    Además aplica el timeout de sesión por rol: si pasó más tiempo del
    permitido para ese rol desde la última actividad, la sesión se limpia
    y `g.user` queda en None (el decorador @requiere_login redirige a /login).
    """
    g.user = None
    g.permisos = set()
    user_id = session.get("user_id")
    if not user_id:
        return
    row = db.fetch_one(
        """
        SELECT u.id_usuario, u.username, u.id_rol, u.activo, r.nombre_rol
        FROM seguridad.usuario u
        JOIN seguridad.rol r USING (id_rol)
        WHERE u.id_usuario = %s AND u.activo = TRUE
        """,
        (user_id,),
    )
    if not row:
        session.clear()
        return

    # Timeout por rol — sliding window. Si expiró, limpiar y cortar.
    timeout = timeout_for_role(row.get("nombre_rol"))
    last_activity = _parse_last_activity(session.get("last_activity"))
    now = _now_utc()
    if last_activity is not None and (now - last_activity) > timeout:
        # Expiró. Vaciamos todo y marcamos g.user=None para que redirija
        # al login. Flasheamos un mensaje suave — no un error, es normal.
        session.clear()
        flash("Tu sesión expiró por inactividad. Ingresá de nuevo.", "info")
        return

    # Actualizar last_activity en cada request. No en endpoints de estáticos
    # o healthcheck — no tiene sentido que un GET /healthz de monitoreo
    # reinicie el contador. Pero los GETs "reales" sí cuentan como actividad.
    if not _is_keepalive_path(request.path):
        session["last_activity"] = now.isoformat()

    g.user = row
    permisos = db.fetch_all(
        "SELECT nombre_opcion FROM seguridad.permiso WHERE id_rol = %s",
        (row["id_rol"],),
    )
    g.permisos = {p["nombre_opcion"] for p in permisos}


def _is_keepalive_path(path: str | None) -> bool:
    """Paths que NO cuentan como actividad del usuario (monitoreo, estáticos)."""
    if not path:
        return False
    return (
        path.startswith("/static/")
        or path.startswith("/healthz")
        or path.startswith("/favicon")
    )


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def requiere_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def requiere_permiso(nombre_opcion: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.get("user"):
                return redirect(url_for("auth.login", next=request.path))
            if nombre_opcion not in g.permisos and "*" not in g.permisos:
                return render_template("403.html", accion=nombre_opcion), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


def tiene_permiso(nombre_opcion: str) -> bool:
    """Jinja helper — used as `{{ tiene_permiso('x.y') }}`."""
    return nombre_opcion in g.get("permisos", set()) or "*" in g.get("permisos", set())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bitácora (auditoría global)
# ---------------------------------------------------------------------------

# Rutas que nunca se auditan (GETs triviales, estáticos, healthcheck).
_BITACORA_SKIP_PREFIXES = (
    "/static/",
    "/favicon",
    "/healthz",
    "/login",
    "/logout",
)


def registrar_bitacora(
    *,
    modulo: str | None = None,
    accion: str | None = None,
    entidad: str | None = None,
    id_entidad: str | int | None = None,
    payload: dict | None = None,
    resumen: str | None = None,
    status_http: int | None = None,
) -> None:
    """Inserta un renglón en la bitácora. No levanta si falla (best-effort)."""
    try:
        usuario_row = g.get("user") or {}
        usuario = usuario_row.get("username") or "anon"
        rol = usuario_row.get("nombre_rol")
        request_id = g.get("request_id")  # puesto en app.before_request
        import json as _json
        db.execute(
            """
            INSERT INTO scintela.bitacora_acciones
                (usuario, rol, ip, metodo, ruta, modulo, accion,
                 entidad, id_entidad, status_http, payload, resumen,
                 request_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s,
                    %s)
            """,
            (
                usuario[:40], rol and rol[:40],
                (request.remote_addr or "")[:45],
                request.method[:8], request.path[:200],
                (modulo or (request.blueprint or ""))[:40],
                (accion or request.endpoint or "")[:40],
                entidad and entidad[:40],
                str(id_entidad)[:60] if id_entidad is not None else None,
                status_http,
                _json.dumps(payload, default=str) if payload else None,
                (resumen or "")[:200] or None,
                request_id[:36] if request_id else None,
            ),
        )
    except Exception:
        # Nunca romper el request por la bitácora.
        pass


def _should_audit(resp_status: int) -> bool:
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return False
    path = request.path or ""
    if any(path.startswith(p) for p in _BITACORA_SKIP_PREFIXES):
        return False
    # No auditamos 4xx/5xx directamente — el usuario puede estar probando.
    # Sí auditamos 200/302 (éxito + redirect post-form).
    return resp_status < 400


def registrar_bitacora_after_request(response):
    """after_request hook: si fue una escritura exitosa, registrar."""
    try:
        if _should_audit(response.status_code):
            form = request.form
            # Extraer un payload sanitizado (sin csrf_token ni passwords).
            blacklist = {"csrf_token", "password", "password_confirm", "token"}
            payload = {
                k: v for k, v in form.items(multi=True) if k not in blacklist
            } if form else None
            registrar_bitacora(
                payload=payload, status_http=response.status_code,
            )
    except Exception:
        pass
    return response


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    "5 per minute; 20 per hour",
    methods=["POST"],
    error_message="Demasiados intentos. Esperá unos minutos y volvé a probar.",
)
def login():
    # Cuando Google OAuth está activo en este deploy, el POST de user/pass
    # queda apagado — la única forma de loguearse es por el botón de Google.
    # El template render del GET ya muestra el botón en lugar del form.
    from flask import current_app as _ca
    if _ca.config.get("GOOGLE_OAUTH_ENABLED") and request.method == "POST":
        flash("El login con usuario y contraseña está deshabilitado en este servidor.", "error")
        return render_template("login.html"), 410

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Ingresá usuario y contraseña.", "error")
            return render_template("login.html"), 400

        # SELECT defensivo: las columnas 2FA + password_debe_cambiar las agregó
        # la migración 0008. Si alguien levanta el app sin correr migrate.py,
        # las cols no existen y el SELECT falla con 500. Intentamos primero con
        # las cols nuevas; si tira UndefinedColumn, fallback al SELECT viejo
        # con defaults None/False — el app sigue funcionando sin 2FA.
        try:
            row = db.fetch_one(
                """
                SELECT id_usuario, username, password_hash, activo,
                       totp_secret, totp_confirmado_en,
                       password_debe_cambiar
                FROM seguridad.usuario
                WHERE lower(username) = %s
                """,
                (username,),
            )
        except Exception as e:
            # psycopg2.errors.UndefinedColumn u otra falla SQL — degradamos.
            import logging
            logging.getLogger("programa_core").warning(
                "login: cols 2FA faltan (migrate.py 0008 pendiente?): %s", e
            )
            row = db.fetch_one(
                """
                SELECT id_usuario, username, password_hash, activo
                FROM seguridad.usuario
                WHERE lower(username) = %s
                """,
                (username,),
            )
            if row:
                row["totp_secret"] = None
                row["totp_confirmado_en"] = None
                row["password_debe_cambiar"] = False
        if not row or not row["activo"]:
            flash("Usuario o contraseña incorrectos.", "error")
            return render_template("login.html"), 401

        hashed = row["password_hash"]
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        if not bcrypt.checkpw(password.encode("utf-8"), hashed):
            flash("Usuario o contraseña incorrectos.", "error")
            return render_template("login.html"), 401

        # 2FA: si el usuario tiene TOTP confirmado, hacemos un login
        # de 2 pasos — dejamos "pending_user_id" en session y redirigimos
        # a /2fa/verify. La sesión completa (user_id real) se setea recién
        # después de validar el código.
        if row.get("totp_confirmado_en") and row.get("totp_secret"):
            session.clear()
            session["pending_user_id"] = row["id_usuario"]
            session["pending_next"] = request.args.get("next") or url_for("dashboard.index")
            return redirect(url_for("two_fa.verify"))

        session.clear()
        session["user_id"] = row["id_usuario"]
        session["last_activity"] = _now_utc().isoformat()
        session.permanent = True

        # Si el admin marcó password_debe_cambiar (creación nueva o policy),
        # redirigimos al cambio obligatorio en vez del dashboard.
        if row.get("password_debe_cambiar"):
            flash("Por seguridad, cambiá tu contraseña antes de seguir.", "info")
            return redirect(url_for("auth.cambiar_password", next=request.args.get("next") or ""))

        next_url = request.args.get("next") or url_for("dashboard.index")
        return redirect(next_url)

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Cambio de contraseña — obligatorio cuando `password_debe_cambiar` es TRUE.
# ---------------------------------------------------------------------------
# Requisitos mínimos de la policy:
#   - mínimo 10 caracteres
#   - al menos una letra y al menos un dígito
#   - distinta a la actual
#
# Deliberadamente NO imponemos reglas complejas (mayúsculas/minúsculas/símbolos)
# — según las guías modernas (NIST SP 800-63B) lo que importa es largo +
# no-reusar + no-comprometida, más que la complejidad de caracteres.

PASSWORD_MIN_LEN = 10


def _valida_password_nueva(password: str) -> str | None:
    """Devuelve None si la password es aceptable, o un mensaje de error."""
    if len(password) < PASSWORD_MIN_LEN:
        return f"La contraseña debe tener al menos {PASSWORD_MIN_LEN} caracteres."
    if not any(c.isalpha() for c in password):
        return "La contraseña debe incluir al menos una letra."
    if not any(c.isdigit() for c in password):
        return "La contraseña debe incluir al menos un número."
    return None


@auth_bp.route("/password/cambiar", methods=["GET", "POST"], endpoint="cambiar_password")
@limiter.limit(
    "10 per hour",
    methods=["POST"],
    error_message="Demasiados intentos. Esperá y volvé a probar.",
)
def cambiar_password():
    """Permite a un usuario logueado cambiar su contraseña.

    Accesible:
      - voluntariamente desde el menú de usuario
      - obligatoriamente tras el login si `password_debe_cambiar` es TRUE
        (el login redirige acá con ?next=... para retomar después).
    """
    if not g.get("user"):
        return redirect(url_for("auth.login"))

    user_id = g.user["id_usuario"]
    # Releemos password_hash y flag actualizado — g.user no lo trae.
    # SELECT defensivo: `password_debe_cambiar` lo agregó migración 0008.
    # Si la cols no existe, fallback al SELECT básico con flag=False.
    try:
        row = db.fetch_one(
            """
            SELECT id_usuario, password_hash, password_debe_cambiar
            FROM seguridad.usuario
            WHERE id_usuario = %s
            """,
            (user_id,),
        )
    except Exception:
        row = db.fetch_one(
            "SELECT id_usuario, password_hash FROM seguridad.usuario WHERE id_usuario = %s",
            (user_id,),
        )
        if row:
            row["password_debe_cambiar"] = False
    if not row:
        session.clear()
        return redirect(url_for("auth.login"))

    next_url = request.args.get("next") or url_for("dashboard.index")

    if request.method == "POST":
        actual = request.form.get("actual") or ""
        nueva = request.form.get("nueva") or ""
        confirm = request.form.get("confirm") or ""

        hashed = row["password_hash"]
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        if not bcrypt.checkpw(actual.encode("utf-8"), hashed):
            flash("La contraseña actual es incorrecta.", "error")
            return render_template("cambiar_password.html", forzado=row["password_debe_cambiar"]), 401

        if nueva != confirm:
            flash("La confirmación no coincide con la contraseña nueva.", "error")
            return render_template("cambiar_password.html", forzado=row["password_debe_cambiar"]), 400

        err = _valida_password_nueva(nueva)
        if err:
            flash(err, "error")
            return render_template("cambiar_password.html", forzado=row["password_debe_cambiar"]), 400

        if bcrypt.checkpw(nueva.encode("utf-8"), hashed):
            flash("La nueva contraseña debe ser distinta de la actual.", "error")
            return render_template("cambiar_password.html", forzado=row["password_debe_cambiar"]), 400

        nuevo_hash = bcrypt.hashpw(nueva.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        db.execute(
            """
            UPDATE seguridad.usuario
               SET password_hash = %s,
                   password_cambio_en = %s,
                   password_debe_cambiar = FALSE
             WHERE id_usuario = %s
            """,
            (nuevo_hash, _now_utc(), user_id),
        )
        flash("Contraseña actualizada.", "info")
        return redirect(next_url)

    return render_template("cambiar_password.html", forzado=row["password_debe_cambiar"])
