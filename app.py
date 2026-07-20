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

    # Calentador de cachés Asinfo (dueña 2026-07-18): refresca las funciones
    # caras al arrancar (post-deploy) y cada 4 min, para que nadie vea la
    # carga fría de 15-21s de /balance y /flujo-produccion. Fail-soft, hilo
    # daemon, apagable con WARMUP_ASINFO=0; no corre bajo pytest.
    try:
        from modules._lib.warmup import start_warmup_thread
        start_warmup_thread()
    except Exception:  # noqa: BLE001 -- el warmup jamás frena el arranque
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

    from modules.precios.views import precios_bp

    app.register_blueprint(precios_bp)

    from modules.facturas.views import facturas_bp

    app.register_blueprint(facturas_bp)

    from modules.cheques.views import cheques_bp

    app.register_blueprint(cheques_bp)

    from modules.bancos.views import bancos_bp

    app.register_blueprint(bancos_bp)

    from modules.compras.views import compras_bp

    app.register_blueprint(compras_bp)

    from modules.tejeduria_asinfo.views import tejeduria_asinfo_bp

    app.register_blueprint(tejeduria_asinfo_bp)

    from modules.stock.views import stock_bp

    app.register_blueprint(stock_bp)

    # TMT 2026-05-22 — blueprint nuevo /stock/asinfo (cantidad stock desde
    # ERP via Metabase). Aislado del stock_bp para no tocar modules/stock/.
    from modules.stock_asinfo.views import stock_asinfo_bp

    app.register_blueprint(stock_asinfo_bp, url_prefix="/stock")

    # TMT 2026-06-09 — /importaciones: importaciones de Asinfo cruzadas con las
    # compras/anticipos del programa por el código que va en la Nota.
    from modules.importaciones.views import importaciones_bp

    app.register_blueprint(importaciones_bp)

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

    from modules.checklist.views import checklist_bp
    from modules.cobranzas.views import cobranzas_bp

    app.register_blueprint(cobranzas_bp)
    # Checklist del día — qué falta cargar en PC vs la operación de ayer
    # (pedido dueña 2026-06-12, transición dBase→PC). TMT.
    app.register_blueprint(checklist_bp)

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

    # banco_v2_view registra los endpoints /conciliacion/banco-v2/* — Reforma
    # Sprint 1 (2026-05-28). Coexiste con /conciliacion/hub vigente hasta swap.
    from modules.conciliacion import banco_v2_view  # noqa: F401
    from modules.conciliacion.views import conciliacion_bp
    # /conciliacion/cambios eliminado 2026-05-29 dueña: 'esta pantalla no
    # sirve para nada'. El historial de matches se ve en /banco-v2/deshacer.

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

    # Aplicar migraciones SQL/Python en 1-click — TMT 2026-05-28.
    # Reemplaza RDP+migrate.py manual por POST a /admin/migraciones (admin).
    from modules.admin_dbase.migraciones_view import bp as admin_migraciones_bp

    app.register_blueprint(admin_migraciones_bp)

    # Deploy 1-click — TMT 2026-05-29. /admin/deploy hace `git pull origin
    # main` y Restart-ScheduledTask en 2 botones, reemplazando el dance
    # SSM/Run Command que la dueña hacía a mano cada push.
    from modules.admin_dbase.deploy_view import bp as admin_deploy_bp

    app.register_blueprint(admin_deploy_bp)

    # Reconciliador POSDAT — TMT 2026-06-05. /admin/posdat-reconcile alinea
    # scintela.posdat con POSDAT.DBF (quirúrgico: UPDATE in-place preservando
    # id_posdat, DELETE de las que sobran salvo linkeadas, INSERT de las que
    # faltan; YY fija baseline=hoy). Dry-run por defecto.
    from modules.admin_dbase.posdat_reconcile_view import bp as posdat_reconcile_bp

    app.register_blueprint(posdat_reconcile_bp)

    # Reconciliador FACTURAS (dry-run) — TMT 2026-06-10. /admin/facturas-reconcile
    # compara scintela.factura con FACTURAS.DBF y bucketé: pendiente de sync /
    # backfill Asinfo / creadas en PC (el sync las borraría) / huérfanas / diffs
    # de cobranza. SOLO LECTURA: el "apply" de facturas es el sync normal.
    from modules.admin_dbase.facturas_reconcile_view import bp as facturas_reconcile_bp

    app.register_blueprint(facturas_reconcile_bp)

    # Comparador sistemático PC vs dBase — TMT 2026-06-10 (pedido dueña:
    # "quiero poder comparar exacto sin sync, se están usando los dos
    # programas"). /admin/dbase-compare: tarball DBFs → 13 checks con reglas
    # PRG + identidad de utilidad (residuo 0 = todo explicado). SOLO LECTURA.
    from modules.admin_dbase.dbase_compare_view import bp as dbase_compare_bp

    app.register_blueprint(dbase_compare_bp)

    # Fechas de depósito desde el dBase — TMT 2026-07-20 (dueña: "¿no podés
    # traer el campo depositado?"). /admin/cheques-fechas-deposito completa
    # SOLO cheque.fechaing (columna Depositado) de cheques B/A sin fecha,
    # leyendo FECHING del CHEQUES.DBF ya subido al comparador. Display-only.
    from modules.admin_dbase.cheques_feching_view import bp as cheques_feching_bp

    app.register_blueprint(cheques_feching_bp)

    # TOTF 1 a 1 — TMT 2026-06-11. /admin/totf-1a1: pareo completo factura
    # por factura (N° SRI) PC vs FACTURAS.DBF, sin truncar, con cross-check
    # de backfill/stat del otro lado. SOLO LECTURA.
    from modules.admin_dbase.totf_1a1_view import bp as totf_1a1_bp

    app.register_blueprint(totf_1a1_bp)

    # Anticipos (scintela.dolares) — TMT 2026-06-11 dueña: sin sync, los
    # anticipos se cargan directo en PC. Alta + cancelación, suma a ANTIC.
    # TMT 2026-07-06 (dueña): "/anticipos/ borrar, tiene que ser /dolares" —
    # alta + cancelar MOVIDOS a modules/dolares; este blueprint queda solo
    # como redirects de compatibilidad (no borrar del disco todavía).
    from modules.anticipos.views import bp as anticipos_bp

    app.register_blueprint(anticipos_bp)

    # Importador de fichas de clientes — TMT 2026-06-06. /admin/clientes-import
    # completa dirección/teléfono/RUC/provincia desde CLIENTES.DBF (que no entra
    # al sync normal) y agrega los clientes que falten. Dry-run por defecto.
    from modules.admin_dbase.clientes_import_view import bp as clientes_import_bp
    from modules.admin_dbase.ficha_asinfo_view import bp as ficha_asinfo_bp

    app.register_blueprint(clientes_import_bp)
    app.register_blueprint(ficha_asinfo_bp)

    # Importador de proveedores desde FABRICA.DBF — TMT 2026-06-19.
    # /admin/proveedores-import crea los proveedores que faltan (BP, AC, AQ…)
    # con nombre/RUC/retenciones del maestro FABRICA. Dry-run por defecto.
    from modules.admin_dbase.proveedores_import_view import bp as proveedores_import_bp

    app.register_blueprint(proveedores_import_bp)

    # Cleanup one-off — marcar facturas Asinfo retroactivas como
    # usuario_crea='asinfo-backfill'. TMT 2026-06-10.
    from modules.admin_dbase.marcar_asinfo_view import bp as marcar_asinfo_bp

    app.register_blueprint(marcar_asinfo_bp)

    # Debug READ-ONLY de facturas en Asinfo (via Metabase DB 2) — TMT
    # 2026-06-12. /admin/debug-asinfo-facturas: investigar atributos de
    # facturas del ERP (vendedor, serie SRI, usuario, estado, forma de
    # pago) sin tocar datos. SOLO LECTURA.
    from modules.admin_dbase.debug_asinfo_facturas_view import bp as debug_asinfo_fact_bp
    from modules.admin_dbase.debug_fabricacion_wip_view import bp as debug_fab_wip_bp

    app.register_blueprint(debug_asinfo_fact_bp)
    app.register_blueprint(debug_fab_wip_bp)

    # Health audit endpoints (Capas 3+4) — usuario_crea audit + utilidad
    # watchdog. JSON-only, para cron / curl manual. TMT 2026-06-10.
    from modules.admin_dbase.health_audit_view import bp as health_audit_bp

    app.register_blueprint(health_audit_bp)

    # Regenerar snapshot scintela.historia. TMT 2026-06-10.
    from modules.admin_dbase.regen_snapshot_view import bp as regen_snapshot_bp

    app.register_blueprint(regen_snapshot_bp)

    # Vincular cheques históricos del dBase a sus facturas — TMT 2026-06-07.
    # /admin/abonos-historicos reconstruye el chequesxfact que el dBase nunca
    # guardó (CHEQUES.DBF no referencia la factura) y recalcula
    # abono = SUM(chequesxfact). Dry-run + confirmar.
    from modules.admin_dbase.abonos_historicos_view import bp as abonos_historicos_bp

    app.register_blueprint(abonos_historicos_bp)

    # Debug YY display-time — TMT 2026-05-28. Endpoint diagnóstico que
    # corre el helper fila por fila y devuelve tracebacks para encontrar
    # qué provoca el 500 de /posdat?tab=yy sin acceso al log del EC2.
    from modules.admin_dbase.debug_yy_view import bp as admin_debug_yy_bp

    app.register_blueprint(admin_debug_yy_bp)

    # Debug ustock=0 live — TMT 2026-06-02. /admin/debug-ustock devuelve
    # JSON con historia[top3], iniciales[mes actual + fallback],
    # kg_facturas_pc, y simulación del vsto final. Sin SSH/SSM.
    from modules.admin_dbase.debug_ustock_view import bp as admin_debug_ustock_bp

    app.register_blueprint(admin_debug_ustock_bp)

    # Diagnóstico pendientes banco — TMT 2026-06-02. /admin/diag-pendientes-banco
    # cuenta duplicados por (no_banco, documento) en banco_historicos_pendientes
    # y muestra ejemplos. El dedupe nuevo (mig 0062) corre al subir extracto;
    # los duplicados del backfill viejo (migs 0056-0058) hay que detectarlos
    # con este endpoint y limpiarlos aparte.
    from modules.conciliacion.diag_view import bp as conciliacion_diag_bp

    app.register_blueprint(conciliacion_diag_bp)

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
