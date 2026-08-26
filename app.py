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

from flask import Flask, flash, g, redirect, request, url_for

import db
import filters
import modo
import registro_erp
import registro_portal
from auth import (
    auth_bp,
    load_logged_in_user,
    registrar_bitacora_after_request,
    tiene_permiso,
)
from extensions import csrf, limiter
from modules._lib import formulas_db

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
    """Devuelve la SECRET_KEY, PERSISTIDA en disco para que sea estable.

    TMT 2026-06-29 (dueña: 'por qué se cierra la sesión'): la cookie de login
    se firma con SECRET_KEY. Si el entorno la regenera en CADA arranque (cada
    deploy mata python y reinicia), todas las cookies dejan de valer y TODOS
    quedan deslogueados. Solución: persistir la clave en un archivo (que NO
    viaja en el tarball del deploy, así sobrevive) y preferir SIEMPRE esa.

    Orden: (1) archivo persistido → (2) SECRET_KEY del env (y se persiste)
    → (3) prod: generar una estable y persistirla. Nunca rota sola.
    """
    import secrets as _secrets

    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "development").lower()
    es_prod = env in ("production", "prod")
    env_key = os.environ.get("SECRET_KEY")
    _log = logging.getLogger("programa_core")

    key_file = os.environ.get("SECRET_KEY_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".secret_key"
    )

    # (1) Clave persistida → manda (estable entre reinicios/deploys).
    try:
        if os.path.exists(key_file):
            persisted = open(key_file, encoding="utf-8").read().strip()
            if len(persisted) >= 32:
                return persisted
    except Exception as e:  # noqa: BLE001
        _log.warning("No pude leer SECRET_KEY persistida (%s): %s", key_file, e)

    # (2) Env válida → usarla y persistirla para la próxima.
    if env_key and (len(env_key) >= 32 or not es_prod):
        try:
            with open(key_file, "w", encoding="utf-8") as f:
                f.write(env_key)
        except Exception as e:  # noqa: BLE001
            _log.warning("No pude persistir SECRET_KEY del env: %s", e)
        return env_key

    # (3) Prod sin clave válida → generar una ESTABLE y persistirla (mejor que
    #     rotar en cada boot, que es justo lo que deslogueaba a todos).
    if es_prod:
        gen = _secrets.token_urlsafe(64)
        try:
            with open(key_file, "w", encoding="utf-8") as f:
                f.write(gen)
            _log.warning(
                "SECRET_KEY no estaba en env; generé y persistí una estable en %s",
                key_file,
            )
        except Exception as e:  # noqa: BLE001
            _log.error("No pude persistir SECRET_KEY generada (%s) — será efímera "
                       "hasta el próximo arranque: %s", key_file, e)
        return gen

    # Dev fallback — warn loudly so nobody ships this.
    _log.warning("SECRET_KEY no definida; usando fallback de desarrollo. NO DEPLOYES CON ESTO.")
    return "dev-only-replace-me"


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
        # TMT 2026-06-11 (dueña): sesiones de 31 días (sliding — Flask refresca
        # la cookie en cada request). El timeout fino por rol vive en auth.py.
        PERMANENT_SESSION_LIFETIME=timedelta(days=31),
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

    # ⭐ El navegador de los PDFs y las fotos, prendido de entrada. TMT
    # 2026-08-26 (dueña): *"podemos hacer más rápido lo de mandar imagen, pdf y
    # whatsapp desde vendedor. tarda mucho tiempo"*. De los 3,5-5,2 s medidos
    # en producción, casi todo era levantar y matar un navegador por archivo.
    # Ahora hay UNO prendido y cada hoja es una pestaña suya.
    #
    # Corre en los DOS procesos —la oficina y el portal del cliente— porque los
    # dos mandan hojas. A diferencia del calentador de Asinfo, esto no le
    # escribe nada a nadie: es un proceso local que se apaga solo a los 15
    # minutos sin uso.
    #
    # ⚠ El hilo NO frena el arranque ni un request: si el navegador no levanta,
    # las hojas siguen saliendo como salían (un navegador por archivo). Ver
    # `modules/_lib/navegador.py`.
    try:
        from modules._lib import navegador
        navegador.arrancar_en_segundo_plano()
    except Exception:  # noqa: BLE001 -- jamás frena el arranque
        pass

    # 🚨 TMT 2026-08-25: el ciclo de fondo corre SÓLO en el programa de la
    # oficina. `run_portal.py` levanta ESTE MISMO código en otro proceso
    # (puerto 5004, misma base), y hasta hoy los dos arrancaban los mismos
    # hilos. Se vio en la traza: guardaba cada foto DOS veces, con segundos de
    # diferencia, porque el freno de cinco minutos era una variable de proceso
    # y cada proceso respetaba el suyo. Lo mismo le pasa a todo lo que se frena
    # con una variable en vez de con la base.
    #
    # ⭐ El portal no necesita nada de esto: las facturas ya entran por la
    # oficina, y el calentador cachea las pantallas caras del ERP, que en el
    # portal ni siquiera están registradas. Un proceso abierto a internet
    # tampoco tiene por qué estar hablándole a Asinfo cada cuatro minutos.
    if not modo.es_portal():
        # Calentador de cachés Asinfo (dueña 2026-07-18): refresca las funciones
        # caras al arrancar (post-deploy) y cada 4 min, para que nadie vea la
        # carga fría de 15-21s de /balance y /flujo-produccion. Fail-soft, hilo
        # daemon, apagable con WARMUP_ASINFO=0; no corre bajo pytest.
        try:
            from modules._lib.warmup import start_warmup_thread
            start_warmup_thread()
        except Exception:  # noqa: BLE001 -- el warmup jamás frena el arranque
            pass

        # Auto-carga de facturas+retenciones del DÍA en segundo plano (dueña
        # 2026-07-23: "no quiero tener que ir a ninguna página"). Corre la misma
        # carga que ya dispara /operaciones y /facturas, pero sola en el servidor
        # cada 2 min — idempotente y fail-soft. Apagable con AUTOCARGA_FACTURAS=0.
        try:
            from modules._lib.autocarga_facturas import start_auto_carga_thread
            start_auto_carga_thread()
        except Exception:  # noqa: BLE001 -- jamás frena el arranque
            pass

    # TMT 2026-05-28 dueña: 'no quiero usar mi compu como sincamos eso'.
    # Si hay un xlsx fresco en data/dbase_snapshots/, lo sincamos UNA VEZ
    # al boot. Marker file con el hash → idempotente entre reboots, pero se
    # vuelve a correr si subimos un xlsx nuevo (hash distinto).
    try:
        from scripts import sync_stat_from_xlsx_boot  # noqa: F401

        sync_stat_from_xlsx_boot.maybe_run_once()
    except Exception:
        # No-op si algo falla — el sync se puede correr a mano por endpoint.
        logging.getLogger("programa_core.boot").exception(
            "sync_stat_from_xlsx_boot falló silenciosamente"
        )

    # Jinja
    filters.register(app)
    app.jinja_env.globals["tiene_permiso"] = tiene_permiso

    # Vocabulario central — un solo lugar para los nombres canónicos.
    # Uso en templates: `{{ L.BANCO_PICHINCHA }}`, `{{ L.label_tipo_compra('H') }}`,
    # `{{ L.TIPOS_COMPRA_LABEL.items() }}`, etc. TMT 2026-05-12.
    import labels as L

    app.jinja_env.globals["L"] = L

    # "← Volver" = la pantalla ANTERIOR con su filtro, no la lista pelada.
    # TMT 2026-08-17 (dueña): *"cuando arme un filtro y clickeo en algo, cuando
    # regreso debería volver a la pantalla anterior con el filtro incluido"*.
    # Ver volver.py — el destino fijo de cada botón queda como respaldo.
    import volver as _volver

    _volver.register(app)

    # --- request-id + timing middleware ------------------------------------
    # request_id: UUID v4 generado en before_request, expuesto como header
    # X-Request-Id y escrito en scintela.bitacora_acciones.request_id. Sirve
    # para correlacionar logs con renglones de bitácora: un usuario reporta
    # "me dio error en las 14:07", buscás en los logs la línea del error,
    # copiás el X-Request-Id y hacés `SELECT … WHERE request_id = '…'` en
    # bitácora para ver exactamente qué intentó hacer y con qué payload.
    _req_log = logging.getLogger("programa_core.req")

    # ⭐ El termómetro de las pantallas (TMT 2026-08-26: *"cómo se podría
    # evaluar las pantallas del programa y hacerlas más rápido"*). Los dos
    # números ya se medían —los ms del request acá abajo y los de cada consulta
    # en `db._t`— y terminaban en un log que vive en el servidor Windows y que
    # no lee nadie. `medidor` los junta en memoria y los muestra en
    # /admin/pantallas. No escribe en la base ni guarda datos de nadie.
    try:
        from modules._lib import medidor as _medidor

        db.OBSERVADOR = _medidor.anotar_consulta
    except Exception:  # noqa: BLE001 -- medir jamás frena el arranque
        _medidor = None

    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()
        if _medidor is not None:
            _medidor.arrancar()
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
        # ⚠ La REGLA (`/facturas/<numf>`) y no la URL: si fuera la URL, cada
        # factura sería una pantalla distinta y no se podría sumar nada. Y sin
        # regla —un 404— no se guarda: no es una pantalla del programa.
        if _medidor is not None and request.url_rule is not None:
            _medidor.cerrar(request.url_rule.rule, request.method, ms,
                            response.status_code)
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

    # Scope de datos de VENDEDORES — TMT 2026-08-03. También DESPUÉS de
    # load_logged_in_user (mira g.user.vend). Un usuario con `vend` cargado
    # sólo puede tocar /mi-cartera; todo lo demás devuelve 404. Para el resto
    # de los usuarios es un no-op exacto. Ver scope_vendedor.py: es un
    # allowlist a propósito (fail-closed), no una lista de rutas prohibidas.
    from scope_vendedor import enforce_scope_vendedor, es_vendedor

    app.before_request(enforce_scope_vendedor)
    # base.html lo usa para no dibujarle el chrome de escritorio (menú con
    # entradas que él no puede abrir) a un vendedor.
    app.jinja_env.globals["es_vendedor"] = es_vendedor

    # Blueprints — TMT 2026-08-24: se registran por MODO. En modo portal las
    # del ERP NO se registran: no existen, así que no hay candado que pueda
    # fallar. Ver modo.py, registro_erp.py y registro_portal.py.
    if modo.es_portal():
        registro_portal.registrar(app)
    else:
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

        # TMT 2026-07-20 (dueña): módulo 2FA BORRADO — pantallas huérfanas
        # (nadie podía llegar a activarlo) y tenía un bug latente en el login.

        registro_erp.registrar(app)

    # Bitácora — after_request hook. Best-effort audit log for every write
    # request (POST/PUT/DELETE/PATCH). MUST be registered AFTER the timing
    # middleware so we don't steal its elapsed-time header, and AFTER all
    # blueprints so any of them can be the audited target.
    app.after_request(registrar_bitacora_after_request)

    # Uso de la app — el hook hermano del de arriba, pero para lo que un
    # vendedor MIRA (los GET). TMT 2026-08-26 (dueña): "¿podríamos medir
    # cuánto usa cada vendedor la aplicación, y qué movimientos hace?". La
    # bitácora contesta la segunda mitad y audita sólo escrituras a propósito;
    # las visitas van a su propia tabla. Ver modules/uso/registro.py.
    #
    # Se registra en los dos modos: sin un usuario con `vend` en la sesión es
    # un no-op exacto, y en modo portal no hay ninguno.
    from modules.uso.registro import registrar_uso_after_request

    app.after_request(registrar_uso_after_request)


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

    # TMT 2026-07-29 (dueña): la CAMPANITA. Se inyecta como FUNCIÓN (no como
    # valor) para que sólo toque la base cuando el template la llama — así no le
    # agrega una query a todas las pantallas de todos los usuarios.
    # TMT 2026-07-30: dejó de ser la campanita de UN proceso ("hacé
    # notificaciones globales") y lee el buzón único `scintela.aviso`
    # (modules/avisos) donde escriben tejeduría, químicos e importaciones.
    @app.context_processor
    def _inject_avisos():
        def _novedades():
            try:
                if not g.get("user"):
                    return {"n": 0, "items": []}
                from modules import avisos as _av

                items = _av.listar(solo_no_leidos=True, limite=15)
                return {"n": len(items), "items": items}
            except Exception:  # noqa: BLE001 -- nunca rompe una pantalla
                return {"n": 0, "items": []}

        def _ventas_hoy_pin():
            """Los kilos de hoy, fijos arriba de la campanita.

            TMT 2026-08-13 (dueña): *"me gustaría que hagas pin acá el de
            ventas"* — el aviso de ventas se leía y se iba; el número del día
            tiene que estar SIEMPRE, aunque no haya novedades. Mismos números
            que el recuadro del inicio (y que el aviso), para que no puedan
            contradecirse. Nunca rompe una pantalla: si algo falla, no se
            muestra el renglón.
            """
            try:
                if not g.get("user"):
                    return None
                from auth import tiene_permiso as _tp
                if not _tp("facturas.ver"):
                    return None
                from modules.facturas.views import resumen_hoy

                # Sin Asinfo: el pin está en TODAS las pantallas y el render no
                # puede quedar colgado del ERP. El despachado lo completa el
                # fetch al abrir la campanita.
                return resumen_hoy(con_despacho=False)
            except Exception:  # noqa: BLE001 -- nunca rompe una pantalla
                return None

        return {"novedades": _novedades, "ventas_hoy_pin": _ventas_hoy_pin}

    # TMT 2026-08-24 — la puerta del programa de la oficina. En modo portal NO
    # se registra: la `/` es del portal, y dejar las dos compitiendo por la
    # misma ruta hace que quién gana dependa del ORDEN en que se registraron,
    # que es exactamente el tipo de cosa que se rompe sola. Ver modo.py.
    if not modo.es_portal():
        @app.route("/")
        def index():
            if not g.get("user"):
                return redirect(url_for("auth.login"))
            return redirect(url_for("dashboard.index"))

    @app.route("/_healthz")
    def healthz():
        """Health check liviano para monitoring externo (Route53/CloudWatch).

        TMT 2026-06-03: documentado en docs/SERVER_AUTO_RECOVERY.md. Sin
        auth, sin DB write. Chequea que el proceso Flask responde Y que el
        pool de DB puede ejecutar `SELECT 1` en <2s. Si DB stuck → 503.

        Uso por monitoring externo: GET /_healthz → 200 healthy, 503 sick.
        """
        from flask import jsonify
        try:
            import db as _db_local
            row = _db_local.fetch_one("SELECT 1 AS ok", ())
            db_ok = bool(row and row.get("ok") == 1)
        except Exception as _e:
            return jsonify({"ok": False, "db": False, "error": str(_e)[:100]}), 503
        if not db_ok:
            return jsonify({"ok": False, "db": False}), 503
        return jsonify({"ok": True, "db": True}), 200

    # --- Error handlers globales --------------------------------------------
    # 404 amigable; 500 genérico con request_id para que el operador pueda
    # pegar el ID corto en la bitácora. 403 sigue lo que ya hace ip_allowlist
    # + @requiere_permiso (renderizan su propio 403.html); no lo sobreescribimos.
    from flask import render_template as _render

    # TMT 2026-08-24 — las páginas de error también van por MODO. `404.html`
    # extiende `base.html`, que es el chrome de la oficina: en el proceso del
    # portal ni `historial.operaciones` ni `auth.logout` existen, así que
    # renderizarla tira BuildError y un 404 se convierte en un 500. Y el
    # portal, que está en internet, va a comerse 404 de robots todo el día.
    _P404 = "portal/404.html" if modo.es_portal() else "404.html"
    _P500 = "portal/500.html" if modo.es_portal() else "500.html"

    @app.errorhandler(404)
    def _not_found(_exc):
        return _render(_P404), 404

    # CSRF vencido — TMT 2026-08-05 (patricio: *"Bad Request. The CSRF tokens
    # do not match"* al intentar entrar). Sin este handler, Flask-WTF devuelve
    # la página cruda de Werkzeug: fondo blanco, texto en inglés, sin un link
    # a ningún lado. Para el que la ve es un CALLEJÓN SIN SALIDA — y encima el
    # remedio (recargar la página de login para que le den un token nuevo) es
    # justo lo que nadie adivina, porque el botón de atrás devuelve la MISMA
    # página vieja con el MISMO token muerto.
    #
    # Un token que no matchea casi siempre significa que la pestaña estaba
    # abierta desde antes. No es un ataque ni un error del que lo escribió:
    # se re-renderiza el login (con token FRESCO, porque el template llama a
    # `csrf_token()` de nuevo) y se le pide que reintente. Así el segundo
    # intento entra solo.
    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _csrf_vencido(_exc):
        flash(
            "La página había quedado abierta un rato largo y se venció. "
            "Escribí tu usuario y contraseña de nuevo.",
            "info",
        )
        if request.path.rstrip("/").endswith("/login"):
            return _render("login.html"), 400
        # En el resto de las pantallas el usuario SÍ está logueado: lo
        # devolvemos a donde estaba en vez de escupirle un 400 pelado.
        destino = request.referrer or url_for("auth.login")
        return redirect(destino), 302

    # La página de login NO se cachea: una copia guardada trae un token de
    # CSRF ya muerto y el POST rebota con el error de arriba. `no-store` es
    # lo que evita que el navegador (o un proxy) la sirva vieja.
    @app.after_request
    def _login_sin_cache(resp):
        if request.path.rstrip("/").endswith("/login"):
            resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

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
        return _render(_P500), 500

    @app.teardown_appcontext
    def _noop(exc):  # pool handles its own lifecycle
        pass

    return app
