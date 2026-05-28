"""Flask application factory.

Wiring:
    - env sanity check (fail fast on missing prod config)
    - db pool init
    - session config + CSRF
    - rate limiter on /login
    - blueprints (auth, dashboard, informes, …)
    - jinja filters + permission helper
    - per-request timing log
"""

import logging
import os
import time
import uuid
from datetime import timedelta

from flask import Flask, g, redirect, request, url_for

import db
import filters
from modules._lib import formulas_db
from auth import (
    auth_bp,
    load_logged_in_user,
    registrar_bitacora_after_request,
    tiene_permiso,
)
from extensions import csrf, limiter

# Slow-request threshold. Override with REQ_SLOW_MS in .env.
REQ_SLOW_MS = int(os.environ.get("REQ_SLOW_MS", "500"))


def _is_uuid_like(value: str) -> bool:
    """True si `value` parece un UUID canónico (36 chars, 4 guiones, hex).

    Aceptamos cualquier versión de UUID (no sólo v4) — el objetivo es filtrar
    basura de un proxy mal configurado, no validar criptográficamente.
    """
    if not value or len(value) != 36:
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _load_secret_key() -> str:
    """Get SECRET_KEY from env. Fail fast in prod, tolerate a dev fallback."""
    key = os.environ.get("SECRET_KEY")
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "development").lower()
    if not key:
        if env in ("production", "prod"):
            raise RuntimeError(
                "SECRET_KEY no está configurada. En producción es obligatoria. "
                "Generá una con: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
            )
        # Dev fallback — warn loudly so nobody ships this.
        logging.getLogger("programa_core").warning(
            "SECRET_KEY no definida; usando fallback de desarrollo. NO DEPLOYES CON ESTO."
        )
        return "dev-only-replace-me"
    if len(key) < 32 and env in ("production", "prod"):
        raise RuntimeError("SECRET_KEY demasiado corta (mínimo 32 caracteres en prod).")
    return key


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Cookies: secure in prod, plain HTTP allowed in dev. Override explicitly
    # with SESSION_COOKIE_SECURE=1 if dev is behind HTTPS.
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "development").lower()
    default_secure = env in ("production", "prod")
    cookie_secure = os.environ.get("SESSION_COOKIE_SECURE")
    if cookie_secure is None:
        cookie_secure = default_secure
    else:
        cookie_secure = cookie_secure.lower() in ("1", "true", "yes", "on")

    app.config.update(
        SECRET_KEY=_load_secret_key(),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
        WTF_CSRF_TIME_LIMIT=None,  # expires with session, not earlier
        JSON_AS_ASCII=False,
    )

    # CSRF — covers every POST/PUT/DELETE form. Templates must include
    # {{ csrf_token() }} in forms (base.html already does this).
    csrf.init_app(app)
    limiter.init_app(app)

    # DB
    db.init_pool()

    # Bridge read-only a formulas_app (mismo cluster RDS, otra DB).
    # No-op si FORMULAS_DATABASE_URL no está seteada — el bridge degrada
    # silenciosamente y los módulos consumidores muestran placeholder.
    formulas_db.init_pool()

    # Jinja
    filters.register(app)
    app.jinja_env.globals["tiene_permiso"] = tiene_permiso

    # Vocabulario central — un solo lugar para los nombres canónicos.
    # Uso en templates: `{{ L.BANCO_PICHINCHA }}`, `{{ L.label_tipo_compra('H') }}`,
    # `{{ L.TIPOS_COMPRA_LABEL.items() }}`, etc. TMT 2026-05-12.
    import labels as L

    app.jinja_env.globals["L"] = L

    # --- request-id + timing middleware ------------------------------------
    # request_id: UUID v4 generado en before_request, expuesto como header
    # X-Request-Id y escrito en scintela.bitacora_acciones.request_id. Sirve
    # para correlacionar logs con renglones de bitácora: un usuario reporta
    # "me dio error en las 14:07", buscás en los logs la línea del error,
    # copiás el X-Request-Id y hacés `SELECT … WHERE request_id = '…'` en
    # bitácora para ver exactamente qué intentó hacer y con qué payload.
    _req_log = logging.getLogger("programa_core.req")

    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()
        # Respetar un X-Request-Id entrante sólo si viene bien formado (36
        # chars, formato UUID). Evita que un cliente inyecte un valor raro
        # o que un proxy intermedio envíe basura.
        incoming = request.headers.get("X-Request-Id", "")
        g.request_id = incoming if _is_uuid_like(incoming) else str(uuid.uuid4())

    @app.after_request
    def _log_request(response):
        # El request_id se emite aunque haya fallado el timer (p.ej. si
        # before_request levantó antes de _start_timer).
        rid = g.get("request_id")
        if rid:
            response.headers["X-Request-Id"] = rid
        t0 = g.pop("_t0", None)
        if t0 is None:
            return response
        ms = (time.perf_counter() - t0) * 1000
        # Always set the header so curl / devtools can see it.
        response.headers["X-Response-Time-ms"] = f"{ms:.0f}"
        # Log slow requests at WARNING (they surface in the log tail).
        # Include request_id so grep puede pivotar a bitácora.
        tag = f"[{rid[:8]}]" if rid else ""
        if ms >= REQ_SLOW_MS:
            _req_log.warning("slow %.0fms %s %s %s", ms, tag, request.method, request.full_path.rstrip("?"))
        else:
            _req_log.info("  %.0fms %s %s %s", ms, tag, request.method, request.full_path.rstrip("?"))
        return response

    # Make sure our loggers write to wherever Flask's does.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Auth hook
    app.before_request(load_logged_in_user)

    # IP allowlist por rol — DEBE ir DESPUÉS de load_logged_in_user porque
    # mira g.user.nombre_rol. Default-allow: sin env var ROLE_IP_ALLOWLIST_X,
    # el rol X pasa igual que siempre. Configurar con
    # ROLE_IP_ALLOWLIST_ACCIONISTA=190.152.1.0/24 etc. (TMT 2026-05-19 v8 —
    # "Dueño" renombrado a "Accionista"; si tenías ROLE_IP_ALLOWLIST_DUENO
    # exportada, renombrala a ROLE_IP_ALLOWLIST_ACCIONISTA o queda inactiva).
    from ip_allowlist import enforce_allowlist

    app.before_request(enforce_allowlist)

    # Blueprints
    app.register_blueprint(auth_bp)

    # Google OAuth — activo sólo si GOOGLE_CLIENT_ID está en env.
    # init_oauth() es no-op si la env var falta (útil para dev local
    # sin OAuth configurado). Cuando está activo, el template del login
    # muestra el botón Google y `auth.login` POST devuelve 410 Gone.
    from modules.auth_google.views import (
        auth_google_bp,
        google_oauth_enabled,
        init_oauth,
    )

    init_oauth(app)
    app.register_blueprint(auth_google_bp)
    app.jinja_env.globals["google_oauth_enabled"] = google_oauth_enabled

    from modules.two_fa.views import two_fa_bp

    app.register_blueprint(two_fa_bp)

    from modules.dashboard.views import dashboard_bp

    app.register_blueprint(dashboard_bp)

    from modules.informes.views import informes_bp

    app.register_blueprint(informes_bp, url_prefix="/informes")

    # TMT 2026-05-22 — blueprint propio (bajo /informes) que cruza
    # tintorería PC vs formulas_app. Aislado del informes_bp para evitar
    # conflictos con cambios en paralelo de modules/informes/views.py.
    from modules.comparativa_tintoreria.views import comparativa_tintoreria_bp

    app.register_blueprint(comparativa_tintoreria_bp, url_prefix="/informes")

    from modules.clientes.views import clientes_bp

    app.register_blueprint(clientes_bp)

    from modules.proveedores.views import proveedores_bp

    app.register_blueprint(proveedores_bp)

    from modules.facturas.views import facturas_bp

    app.register_blueprint(facturas_bp)

    from modules.cheques.views import cheques_bp

    app.register_blueprint(cheques_bp)

    from modules.bancos.views import bancos_bp

    app.register_blueprint(bancos_bp)

    from modules.compras.views import compras_bp

    app.register_blueprint(compras_bp)

    from modules.stock.views import stock_bp

    app.register_blueprint(stock_bp)

    # TMT 2026-05-22 — blueprint nuevo /stock/asinfo (cantidad stock desde
    # ERP via Metabase). Aislado del stock_bp para no tocar modules/stock/.
    from modules.stock_asinfo.views import stock_asinfo_bp

    app.register_blueprint(stock_asinfo_bp, url_prefix="/stock")

    from modules.retenciones.views import retenciones_bp

    app.register_blueprint(retenciones_bp)

    from modules.caja.views import caja_bp

    app.register_blueprint(caja_bp)

    from modules.capital.views import capital_bp

    app.register_blueprint(capital_bp)

    from modules.provisiones.views import provisiones_bp

    app.register_blueprint(provisiones_bp)

    from modules.proformas.views import proformas_bp

    app.register_blueprint(proformas_bp)

    from modules.posdat.views import posdat_bp

    app.register_blueprint(posdat_bp)

    from modules.dolares.views import dolares_bp

    app.register_blueprint(dolares_bp)

    from modules.cartera.views import cartera_bp

    app.register_blueprint(cartera_bp)

    from modules.historial.views import historial_bp

    app.register_blueprint(historial_bp)

    from modules.cobranzas.views import cobranzas_bp

    app.register_blueprint(cobranzas_bp)

    from modules.comisiones.views import comisiones_bp

    app.register_blueprint(comisiones_bp)

    from modules.gastos.views import gastos_bp

    app.register_blueprint(gastos_bp)

    from modules.retiros.views import retiros_bp

    app.register_blueprint(retiros_bp)

    from modules.activos.views import activos_bp

    app.register_blueprint(activos_bp)

    from modules.iniciales.views import iniciales_bp

    app.register_blueprint(iniciales_bp)

    from modules.bitacora.views import bitacora_bp

    app.register_blueprint(bitacora_bp)

    from modules.periodos.views import periodos_bp

    app.register_blueprint(periodos_bp)

    from modules.usuarios.views import usuarios_bp

    app.register_blueprint(usuarios_bp)

    from modules.sri.views import sri_bp

    app.register_blueprint(sri_bp)

    from modules.costos_ot.views import costos_ot_bp

    app.register_blueprint(costos_ot_bp)

    from modules.conciliacion.views import conciliacion_bp

    app.register_blueprint(conciliacion_bp)

    from modules.healthz.views import healthz_bp

    app.register_blueprint(healthz_bp)

    # Diagnóstico de bridges externos — admin-only, no escribe nada.
    from modules.diag.views import bp as diag_bp

    app.register_blueprint(diag_bp)

    # Sync dBase en 1-click — TMT 2026-05-28. Reemplaza el dance manual
    # CloudShell+S3+SSM por POST a /admin/dbase-sync (admin-only).
    from modules.admin_dbase.views import bp as admin_dbase_bp

    app.register_blueprint(admin_dbase_bp)

    # Auto-match xlsx → scintela.transacciones_bancarias — TMT 2026-05-28.
    # Dueña: "conecta uno con uno me da igual" — endpoint que parsea xlsx y
    # crea matches por (fecha,monto,tipo) con tolerancia de centavo.
    from modules.admin_dbase.auto_match_view import bp as admin_automatch_bp

    app.register_blueprint(admin_automatch_bp)

    # Balance audit: PC vs Banco con desglose por categoría — TMT 2026-05-28.
    from modules.admin_dbase.balance_view import bp as admin_balance_bp

    app.register_blueprint(admin_balance_bp)

    # Bitácora — after_request hook. Best-effort audit log for every write
    # request (POST/PUT/DELETE/PATCH). MUST be registered AFTER the timing
    # middleware so we don't steal its elapsed-time header, and AFTER all
    # blueprints so any of them can be the audited target.
    app.after_request(registrar_bitacora_after_request)

    # `g.now` — datetime local de inicio del request, accesible desde
    # cualquier template (saludo del dashboard, etc).
    @app.before_request
    def _inject_now():
        from datetime import datetime

        g.now = datetime.now()

    # Recientes del usuario — expuestos globalmente en templates como
    # `recientes_usuario` (lista de {tipo,id_ref,etiqueta,tocado_en}).
    # Best-effort — si la tabla no existe o el usuario es anon, devuelve [].
    @app.context_processor
    def _inject_recientes():
        try:
            if not g.get("user"):
                return {"recientes_usuario": []}
            from modules.recientes import queries as rec

            return {"recientes_usuario": rec.listar_recientes(limite=5)}
        except Exception:
            return {"recientes_usuario": []}

    @app.route("/")
    def index():
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        return redirect(url_for("dashboard.index"))

    # --- Error handlers globales --------------------------------------------
    # 404 amigable; 500 genérico con request_id para que el operador pueda
    # pegar el ID corto en la bitácora. 403 sigue lo que ya hace ip_allowlist
    # + @requiere_permiso (renderizan su propio 403.html); no lo sobreescribimos.
    from flask import render_template as _render

    @app.errorhandler(404)
    def _not_found(_exc):
        return _render("404.html"), 404

    @app.errorhandler(500)
    def _internal_error(exc):
        # Siempre loggear con stack — es el único lugar donde el operador
        # puede pivotar a la causa.
        logging.getLogger("programa_core").exception(
            "unhandled 500 [%s] %s %s",
            g.get("request_id", "?")[:8],
            request.method,
            request.path,
        )
        return _render("500.html"), 500

    @app.teardown_appcontext
    def _noop(exc):  # pool handles its own lifecycle
        pass

    return app
