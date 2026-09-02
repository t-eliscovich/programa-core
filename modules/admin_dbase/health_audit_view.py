"""Endpoints de health/audit — Capas 3+4 de protección.

Capa 3: /admin/health/usuario-crea-audit
    Cuenta filas con usuario_crea "huérfano" (no en whitelist) en los
    últimos N días. Si aparece >0, hay un endpoint que se olvidó del marker
    canónico (asinfo-backfill / dbf-import / dueña-web-known-users).

Capa 4: /admin/health/utilidad-watchdog
    Compara utilidad LIVE vs PREVIA snapshot. Alerta si:
      - delta utilidad > $200k (absoluto) o > 50% (relativo)
      - stock terminado_kg subió >10k vs snapshot
      - TOTF subió >$100k vs snapshot sin facturas backfill nuevas

Ambos endpoints devuelven JSON con `{"ok": bool, "alerts": [...], "stats": {...}}`
para que un cron pueda parsearlos.

TMT 2026-06-10.
"""
from __future__ import annotations

import logging
from datetime import date as _dt_date

from flask import Blueprint, jsonify, request

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("health_audit", __name__, url_prefix="/admin/health")

_LOG_REPETIDOS = logging.getLogger("programa_core.health.codigos_repetidos")


# Whitelist de usuario_crea conocidos. Cualquier otro es sospechoso.
# - asinfo-backfill: cargas Asinfo (forward fix + trigger lo aseguran).
# - dbf-import: sync dBase canónico.
# - web: fallback default cuando el flask user es None.
# - <usernames> conocidos del equipo: tamara, andres, alex (cargas manuales
#   legítimas — son las facturas que SE CARGAN A PROPÓSITO en el sistema
#   mediante el form normal /facturas/nueva, NO via Asinfo).
_USUARIOS_CONOCIDOS = {
    "asinfo-backfill",
    "asinfo-carga",
    "asinfo-fantasma",  # mig 0097: estado=0 Asinfo anuladas (stat X)
    "dbf-import",
    "web",
    "tamara",
    "andres",
    "alex",
    "auto",
    "asinfo",
    "formulas-auto",  # puente compras de químicos formulas→PC (cron diario)
    # TMT 2026-07-31 — markers que ya estaban en producción y no figuraban acá,
    # así que el health quedaba en ok=false todos los días por un falso
    # positivo. Cada marker nuevo hay que agregarlo ACÁ el mismo día.
    "federico",           # usuario real (faltaba; tamara/andres/alex sí estaban)
    "bap-auto",           # BAP automático: anticipo USD → compra al recibirse
    "asinfo-tejeduria",   # carga automática de producción de tejeduría
    "asinfo-hilo-local",  # carga automática de compras de hilo local
    "snapshot-diario",    # foto diaria del balance (health)
}

# Prefijos legítimos (marker + usuario dinámico). El puente de compras usa
# 'formulas-<user>' cuando se sincroniza a mano desde /compras/desde-formulas
# (TMT 2026-07-17, shipped 6fb2b55).
_PREFIJOS_CONOCIDOS = ("formulas-",)

# Si una factura tiene este formato en numf_completo, es Asinfo SI o SI.
_REGEX_NUMF_ASINFO = "^[0-9]{3}-[0-9]{3}-[0-9]{9}$"


# ---------------------------------------------------------------------------
# Capa 3: usuario-crea-audit
# ---------------------------------------------------------------------------


@bp.route("/usuario-crea-audit", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def usuario_crea_audit():
    """JSON con anomalías de usuario_crea en los últimos 7 días."""
    alerts = []
    stats = {}

    # 1. Facturas con formato Asinfo pero usuario_crea NO 'asinfo-backfill'
    #    (= el bug original que cazamos hoy).
    try:
        row = db.fetch_one(
            f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(saldo), 0) AS sum_saldo
              FROM scintela.factura
             WHERE fecha_crea >= (CURRENT_DATE - INTERVAL '7 days')
               AND numf_completo ~ '{_REGEX_NUMF_ASINFO}'
               AND COALESCE(usuario_crea, '') NOT IN
                   ('asinfo-backfill', 'asinfo-carga', 'asinfo-fantasma',
                    'dbf-import')
            """
        ) or {}
        n = int(row.get("n") or 0)
        sum_saldo = float(row.get("sum_saldo") or 0)
        stats["facturas_asinfo_sin_marker"] = {"n": n, "sum_saldo": sum_saldo}
        if n > 0:
            alerts.append({
                "severity": "high",
                "category": "facturas_asinfo_sin_marker",
                "msg": (
                    f"{n} facturas con formato Asinfo (numf_completo "
                    f"XXX-XXX-XXXXXXXXX) tienen usuario_crea != "
                    f"'asinfo-backfill'. SUM(saldo)={sum_saldo:.2f}. "
                    f"Estas filas SUMAN a cartera/utilidad LIVE — bug del "
                    f"2026-06-10 reabierto. Correr /admin/marcar-asinfo-hoy."
                ),
            })
    except Exception as e:
        alerts.append({"severity": "error", "category": "query_failed",
                       "msg": f"facturas: {e}"})

    # 2. Filas con usuario_crea "huérfano" en factura/compra/dolares en
    #    los últimos 7 días.
    for tabla, sum_col in (
        ("factura", "importe"),
        ("compra", "importe"),
        ("dolares", "importe"),
    ):
        try:
            placeholders = ",".join(f"'{u}'" for u in _USUARIOS_CONOCIDOS)
            prefijos_sql = " ".join(
                f"AND COALESCE(usuario_crea, '') NOT LIKE '{pref}%'"
                for pref in _PREFIJOS_CONOCIDOS
            )
            row = db.fetch_one(
                f"""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM({sum_col}), 0) AS s,
                       array_agg(DISTINCT usuario_crea) AS usuarios
                  FROM scintela.{tabla}
                 WHERE fecha_crea >= (CURRENT_DATE - INTERVAL '7 days')
                   AND COALESCE(usuario_crea, '') NOT IN ({placeholders})
                   {prefijos_sql}
                """
            ) or {}
            n = int(row.get("n") or 0)
            usuarios_raros = row.get("usuarios") or []
            stats[f"{tabla}_usuario_huerfano"] = {
                "n": n,
                "sum": float(row.get("s") or 0),
                "usuarios_raros": [u for u in usuarios_raros if u],
            }
            if n > 0:
                alerts.append({
                    "severity": "medium",
                    "category": f"{tabla}_usuario_huerfano",
                    "msg": (
                        f"{tabla}: {n} filas con usuario_crea no canónico "
                        f"(usuarios: {usuarios_raros}). Sum={row.get('s'):.2f}. "
                        f"Verificar si son markers nuevos legítimos (agregar "
                        f"a whitelist _USUARIOS_CONOCIDOS) o un endpoint nuevo "
                        f"que omite el marker."
                    ),
                })
        except Exception as e:
            alerts.append({"severity": "error", "category": "query_failed",
                           "msg": f"{tabla}: {e}"})

    return jsonify({
        "ok": len(alerts) == 0,
        "alerts": alerts,
        "stats": stats,
        "whitelist": sorted(_USUARIOS_CONOCIDOS),
    })


# ---------------------------------------------------------------------------
# Capa 4: utilidad-watchdog
# ---------------------------------------------------------------------------


@bp.route("/utilidad-watchdog", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def utilidad_watchdog():
    """JSON con alertas si utilidad / cartera / stock LIVE difieren mucho del
    snapshot PREVIA (último snapshot de scintela.historia del mes actual).
    """
    alerts = []
    stats = {}

    # 1. Obtener snapshot PREVIA (último del mes actual, no del cierre del
    #    mes anterior).
    snap = db.fetch_one(
        """
        SELECT fecha, fecha_crea, patrimonio, banco, cart, ustock,
               deuda, anticipos
          FROM scintela.historia
         WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
           AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
         ORDER BY fecha_crea DESC
         LIMIT 1
        """
    )

    if not snap:
        alerts.append({
            "severity": "low",
            "category": "no_snapshot_mes_actual",
            "msg": (
                "No hay snapshot del mes en curso en scintela.historia. "
                "Watchdog no puede comparar."
            ),
        })
        return jsonify({"ok": True, "alerts": alerts, "stats": stats})

    stats["snapshot_fecha"] = str(snap.get("fecha"))
    stats["snapshot_patrimonio"] = float(snap.get("patrimonio") or 0)
    stats["snapshot_cart"] = float(snap.get("cart") or 0)
    stats["snapshot_ustock"] = float(snap.get("ustock") or 0)

    # 2. Calcular utilidad/patrim/cart/stock LIVE actuales.
    try:
        from modules.informes import queries as iq
        balance = iq.informe_balance()
        comp = balance.get("diagnostico", {}).get("componentes", {})

        # `componentes.patr` es el patrimonio BRUTO: URET (los dividendos del
        # mes) vive DENTRO del Total Activo, así que patr los incluye. En
        # cambio `historia.patrimonio` se guarda NETO — crear_snapshot_diario
        # escribe patr − uret, igual que el PRG (línea 1347: REPLA PATRIMONIO
        # WITH PATR-URET). Comparar uno contra el otro daba un delta inflado
        # por el total de retiros del mes: el 2026-07-31 marcaba +153.772
        # cuando el patrimonio real había BAJADO 57.800 (los retiros del mes
        # eran 211.393). La alerta de $200k saltaba sola a fin de mes.
        # TMT 2026-07-31.
        live_uret = float(comp.get("uret") or 0)
        live_patr = float(comp.get("patr") or 0) - live_uret
        live_cart = float(comp.get("cart") or 0)
        live_vsto = float(comp.get("vsto") or 0)
        live_utilidad = float(comp.get("utilidad") or 0)

        stats["live_uret"] = live_uret
        stats["live_patrimonio"] = live_patr
        stats["live_cart"] = live_cart
        stats["live_vsto"] = live_vsto
        stats["live_utilidad"] = live_utilidad

        # 3. Comparar y alertar.
        snap_patrim = float(snap.get("patrimonio") or 0)
        snap_cart = float(snap.get("cart") or 0)
        snap_ustock = float(snap.get("ustock") or 0)

        d_patrim = live_patr - snap_patrim
        d_cart = live_cart - snap_cart
        d_stock = live_vsto - snap_ustock

        stats["delta_patrimonio"] = d_patrim
        stats["delta_cart"] = d_cart
        stats["delta_stock"] = d_stock

        # Threshold absoluto $200k para delta patrimonio (= utilidad mes en curso).
        if abs(d_patrim) >= 200_000:
            alerts.append({
                "severity": "high",
                "category": "delta_patrimonio_alto",
                "msg": (
                    f"Patrimonio LIVE − snapshot PREVIA = {d_patrim:+,.0f}. "
                    f"Supera el threshold $200k. La utilidad LIVE podría "
                    f"estar inflada por carga Asinfo sin marker."
                ),
            })

        # Cartera no debería subir > $100k entre snapshot y live.
        if d_cart >= 100_000:
            alerts.append({
                "severity": "high",
                "category": "delta_cartera_alto",
                "msg": (
                    f"Cartera LIVE − snapshot = {d_cart:+,.0f}. >$100k. "
                    f"Probable carga Asinfo de facturas sin marker (bug "
                    f"2026-06-10). Correr /admin/marcar-asinfo-hoy."
                ),
            })

        # Stock no debería subir > $50k en un día (re-valoración mensual normal).
        if d_stock >= 50_000:
            alerts.append({
                "severity": "medium",
                "category": "delta_stock_alto",
                "msg": (
                    f"Stock LIVE − snapshot = {d_stock:+,.0f}. >$50k. "
                    f"Verificar compras grandes del día o si h_terminado_kg "
                    f"está usando ventas filtradas en vez de físicas."
                ),
            })

    except Exception as e:
        alerts.append({
            "severity": "error",
            "category": "balance_query_failed",
            "msg": f"informe_balance() falló: {e}",
        })

    return jsonify({
        "ok": len(alerts) == 0,
        "alerts": alerts,
        "stats": stats,
    })


# ---------------------------------------------------------------------------
# Diagnóstico: compras tipo=K kg>0 del último mes (paso tejeduría manual)
# ---------------------------------------------------------------------------


@bp.route("/compras-tipo-k-detalle", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def compras_tipo_k_detalle():
    """Lista las últimas 200 filas de scintela.compra con tipo='K' AND kg>0
    (= producción tejeduría) con usuario_crea. Para identificar QUÉ flow
    está cargando estas filas (Asinfo backfill / dbf-import / carga web manual).

    Sin parámetros — devuelve mes en curso. Read-only.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT id_compra, fecha, fecha_crea, codigo_prov, tipo, kg,
                   importe, concepto, comprobante, numero, usuario_crea,
                   stat
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(kg, 0) > 0
               AND fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
                                     - INTERVAL '1 month'
             ORDER BY fecha_crea DESC, id_compra DESC
             LIMIT 200
            """,
        ) or []
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Agregados por usuario_crea
    by_user = {}
    by_prov = {}
    by_endpoint_hint = {}
    for r in rows:
        u = (r.get("usuario_crea") or "(null)")
        by_user.setdefault(u, {"n": 0, "kg": 0.0, "importe": 0.0})
        by_user[u]["n"] += 1
        by_user[u]["kg"] += float(r.get("kg") or 0)
        by_user[u]["importe"] += float(r.get("importe") or 0)

        p = (r.get("codigo_prov") or "(null)")
        by_prov.setdefault(p, {"n": 0, "kg": 0.0, "importe": 0.0})
        by_prov[p]["n"] += 1
        by_prov[p]["kg"] += float(r.get("kg") or 0)
        by_prov[p]["importe"] += float(r.get("importe") or 0)

    # Snapshot de los 10 más recientes para inspección visual
    recientes = []
    for r in rows[:10]:
        recientes.append({
            "id_compra": r["id_compra"],
            "fecha": str(r.get("fecha")),
            "fecha_crea": (r["fecha_crea"].isoformat()
                           if r.get("fecha_crea") else None),
            "codigo_prov": r.get("codigo_prov"),
            "kg": float(r.get("kg") or 0),
            "importe": float(r.get("importe") or 0),
            "concepto": (r.get("concepto") or "")[:60],
            "comprobante": r.get("comprobante"),
            "numero": r.get("numero"),
            "usuario_crea": r.get("usuario_crea"),
            "stat": r.get("stat"),
        })

    return jsonify({
        "ok": True,
        "total_rows": len(rows),
        "by_usuario_crea": by_user,
        "by_codigo_prov": by_prov,
        "ejemplos_recientes": recientes,
    })


# ---------------------------------------------------------------------------
# Cartera coherence: balance TOTF/TOTC == lista (sin toggle)
# ---------------------------------------------------------------------------


@bp.route("/cartera-coherence", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def cartera_coherence():
    """Compara los totales de `/informes/balance` (totf/totc) contra el
    listado UI sin toggle de backfill. Si difieren >$1, alerta.

    TMT 2026-06-10 — capa de coherencia post-toggle. Sin el toggle, listado
    debe matchear balance al centavo (modulo redondeo). Si difieren, algo
    se desincronizó (ej. un endpoint nuevo se olvidó de aplicar el filtro).
    """
    alerts = []
    stats = {}

    try:
        from modules.facturas import queries as fq
        from modules.informes import queries as iq

        # Balance values (con filtro backfill aplicado)
        totf_balance = iq.totf()
        totc_balance = iq.totc()

        # Lista values — debe coincidir con balance (sin filtros backfill,
        # post-revert TMT 2026-06-10).
        totf_lista = fq.contar_filtrado(
            vista="cartera",
        ).get("total_saldo", 0.0)

        # Para cheques: query directa (no hay un `contar_filtrado` en
        # cheques pero podemos sumar manualmente vía sum SQL).
        row_chq = db.fetch_one(
            """
            SELECT COALESCE(SUM(importe), 0) AS total
              FROM scintela.cheque
             WHERE stat IN ('Z','1','2','3','P','D')
            """,
        )
        totc_lista = float(row_chq["total"] or 0) if row_chq else 0.0

        stats["totf_balance"] = totf_balance
        stats["totf_lista"] = totf_lista
        stats["totc_balance"] = totc_balance
        stats["totc_lista"] = totc_lista
        stats["delta_totf"] = totf_balance - totf_lista
        stats["delta_totc"] = totc_balance - totc_lista

        # Tolerancia $1 absoluto (redondeo SQL/Python).
        if abs(stats["delta_totf"]) >= 1.0:
            alerts.append({
                "severity": "high",
                "category": "totf_mismatch",
                "msg": (
                    f"TOTF balance ({totf_balance:.2f}) != lista filtrada "
                    f"({totf_lista:.2f}). Δ = {stats['delta_totf']:+,.2f}. "
                    f"Las queries del listado y del balance no están en "
                    f"sintonía — buscar query que se olvidó del filtro "
                    f"asinfo-backfill o quita filtros stat IN."
                ),
            })
        if abs(stats["delta_totc"]) >= 1.0:
            alerts.append({
                "severity": "high",
                "category": "totc_mismatch",
                "msg": (
                    f"TOTC balance ({totc_balance:.2f}) != lista filtrada "
                    f"({totc_lista:.2f}). Δ = {stats['delta_totc']:+,.2f}."
                ),
            })
    except Exception as e:
        alerts.append({"severity": "error", "category": "query_failed",
                       "msg": str(e)})

    return jsonify({
        "ok": len(alerts) == 0,
        "alerts": alerts,
        "stats": stats,
    })


# ---------------------------------------------------------------------------
def ejecutar_foto_diaria() -> dict:
    """Rollover + WRITE-BACK de iniciales + FOTO DIARIA del balance vivo,
    validada contra el día anterior. Devuelve {"ok", "alerts", "stats"}.

    Lógica compartida entre la pantalla `/admin/health/snapshot-diario`
    (visita manual, requiere login) y `scripts/foto_diaria_cron.py` (el
    Scheduled Task del EC2, sin sesión -- ver skill intela-aws-deploy).
    TMT 2026-09-02: hasta acá, la ÚNICA forma de que esto corriera era que
    alguien visitara a mano una pantalla de health -- en los 31 días de
    agosto 2026 nadie lo hizo ni una vez, así que ningún día tuvo foto y el
    cierre de mes cayó en la reconstrucción aproximada en vez de la foto
    real (ver memoria project_2026_09_01_cierre_agosto_roto_y_hardening).
    Esta función existe para que el cron pueda llamar EXACTAMENTE la misma
    lógica sin pasar por Flask/login.

    Alerta si:
      - el patrimonio pega un salto > $500k día a día (posible bug de cálculo)
      - el stock (USTOCK) salta > 5% día a día
      - el stock de TERMINADO/USTOCK sale en 0 (bug de iniciales/mes)
      - el patrimonio sale <= 0

    Nunca levanta -- cualquier error de una etapa queda en "alerts", nunca
    en una excepción sin atrapar (el cron necesita poder decidir el exit
    code sin un try/except propio).
    """
    from modules.informes.queries import (
        crear_snapshot_diario,
        rollover_y_writeback_iniciales,
    )

    alerts = []
    stats = {}

    # 1) ROLLOVER + WRITE-BACK de INICIALES (replica el cierre de mes del dBase):
    #    crea la fila del mes si falta y escribe el stock de cierre vivo, para
    #    que PC no dependa de que el dBase abra el 1° de mes.
    try:
        roll = rollover_y_writeback_iniciales()
        stats["rollover"] = roll
        if roll.get("rollover"):
            alerts.append(
                f"ROLLOVER: se creó la fila de INICIALES del mes {roll.get('fecha')} "
                f"copiando el cierre de {roll.get('rollover_desde')} (era el paso "
                "que el dBase hace al abrir el 1° de mes)."
            )
        if roll.get("rollover_error"):
            alerts.append(f"ROLLOVER no pudo crear la fila del mes: {roll['rollover_error']}")
        if roll.get("writeback_error"):
            alerts.append(f"WRITE-BACK falló: {roll['writeback_error']}")
    except Exception as e:  # noqa: BLE001
        alerts.append(f"rollover/writeback iniciales falló: {e}")

    # 2) FOTO DIARIA
    try:
        snap = crear_snapshot_diario()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "alerts": alerts + [f"snapshot diario falló: {e}"], "stats": stats}

    stats["hoy"] = snap
    hoy_patr = float(snap.get("patrimonio") or 0)
    hoy_ustock = float(snap.get("ustock") or 0)

    # Guardas absolutas sobre la foto de hoy
    if hoy_patr <= 0:
        alerts.append(f"Patrimonio de hoy <= 0 ({hoy_patr:,.0f}) — cálculo roto.")
    if hoy_ustock <= 0:
        alerts.append("Stock (USTOCK) de hoy en 0 — probable iniciales del mes sin cargar.")

    # Comparar contra la foto DIARIA anterior (día previo con snapshot-diario)
    prev = db.fetch_one(
        """
        SELECT fecha, patrimonio, ustock
          FROM scintela.historia
         WHERE usuario_crea = 'snapshot-diario'
           AND fecha < %s
         ORDER BY fecha DESC
         LIMIT 1
        """,
        (snap.get("fecha"),),
    )
    if prev:
        p_patr = float(prev.get("patrimonio") or 0)
        p_ustock = float(prev.get("ustock") or 0)
        d_patr = hoy_patr - p_patr
        stats["ayer"] = {"fecha": str(prev.get("fecha")), "patrimonio": p_patr, "ustock": p_ustock}
        stats["delta_patrimonio"] = d_patr
        if abs(d_patr) > 500_000:
            alerts.append(
                f"Patrimonio saltó {d_patr:+,.0f} vs {prev.get('fecha')} — revisar (umbral $500k)."
            )
        if p_ustock > 0 and abs(hoy_ustock - p_ustock) / p_ustock > 0.05:
            alerts.append(
                f"Stock saltó {hoy_ustock - p_ustock:+,.0f} ({100*(hoy_ustock-p_ustock)/p_ustock:+.1f}%) "
                f"vs {prev.get('fecha')} — revisar (umbral 5%)."
            )
    else:
        stats["ayer"] = None

    return {"ok": len(alerts) == 0, "alerts": alerts, "stats": stats}


@bp.route("/snapshot-diario", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def snapshot_diario_health():
    """Toma la FOTO DIARIA del balance vivo y la valida contra el día anterior.

    Esto es lo que independiza a PC del dBase: capturar cada día en vivo (cuando
    cartera/anticipos/stock están frescos) para que el cierre de mes sea, sin
    reconstruir nada, la foto del último día. Corre por el cron del health
    (`scripts/foto_diaria_cron.py`, Scheduled Task en el EC2) -- esta ruta es
    la versión "visitar a mano" de la MISMA lógica, ver `ejecutar_foto_diaria`.
    """
    return jsonify(ejecutar_foto_diaria())


@bp.route("/metabase", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def metabase_bitacora():
    """Las últimas consultas a Asinfo/Fórmulas: cuánto tardaron y cuáles fallaron.

    TMT 2026-07-31. Cuando la utilidad se movía no había forma de saber si el
    puente había contestado — el fallo se loguea por WARNING y ese log, en el
    servidor, no lo lee nadie. Esto es esa respuesta, en una pantalla.

    Sólo lectura, y vive en memoria: se pierde al reiniciar la app. Es para
    mirar AHORA, cuando el número se movió hace un minuto.
    """
    from modules._lib import metabase_client

    filas = metabase_client.bitacora()
    fallos = [f for f in filas if not f.get("ok")]
    lentas = sorted(filas, key=lambda f: -(f.get("ms") or 0))[:5]
    tiempos = sorted((f.get("ms") or 0) for f in filas)
    return jsonify({
        "ok": not fallos,
        "timeout_configurado_secs": metabase_client._timeout_secs(),
        "n_consultas": len(filas),
        "n_fallos": len(fallos),
        "ms_mediana": (tiempos[len(tiempos) // 2] if tiempos else None),
        "ms_maximo": (tiempos[-1] if tiempos else None),
        "las_5_mas_lentas": lentas,
        "fallos": fallos,
        "todas": filas,
    })


@bp.route("/cron-status", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def cron_status():
    """Huella del cron diario (procesa_provisiones_mensual) en la DB — para saber
    si corre, sin acceso al Task Scheduler del EC2. Muestra: las corridas
    registradas en ejecuciones_tareas (cuándo corrió cada tarea) + las fotos
    diarias creadas (una por día si el cron corre) + los rollover-pc."""
    tareas = db.fetch_all(
        """
        SELECT tarea, periodo, estado, iniciado_en, terminado_en, host
          FROM scintela.ejecuciones_tareas
         ORDER BY COALESCE(terminado_en, iniciado_en) DESC NULLS LAST
         LIMIT 15
        """
    ) or []
    fotos = db.fetch_all(
        """
        SELECT fecha, fecha_crea
          FROM scintela.historia
         WHERE usuario_crea = 'snapshot-diario'
         ORDER BY fecha_crea DESC
         LIMIT 10
        """
    ) or []
    rollovers = db.fetch_all(
        """
        SELECT mesnom, yy, mesnum
          FROM scintela.iniciales
         WHERE usuario_crea = 'rollover-pc'
         ORDER BY id_iniciales DESC LIMIT 5
        """
    ) or []

    def _clean(rows):
        return [{k: str(v) for k, v in r.items()} for r in rows]

    return jsonify({
        "ejecuciones_tareas": _clean(tareas),
        "fotos_diarias_snapshot_diario": _clean(fotos),
        "rollovers_pc": _clean(rollovers),
        "nota": ("ejecuciones_tareas se trackea por MES (1 fila/tarea/período), "
                 "así que muestra cuándo corrió el cron ese mes, no cada día. "
                 "Las fotos_diarias sí son 1 por día — si el cron corre mi tarea "
                 "diaria, van a ir apareciendo con fecha_crea a la hora del cron."),
    })


def se_puede_comparar_la_foto(hoy) -> bool:
    """¿El Δ contra la foto guardada dice algo, o hay que callarlo?

    Sólo el ÚLTIMO DÍA DEL MES. Ese día el mes que cierra es el mes en curso, y
    `crear_snapshot_historia` lo calcula con el balance VIVO. Cualquier otro día
    cierra un mes pasado y lo reconstruye con `informe_balance_as_of`, que
    devuelve una CARTERA que no cierra: medido el 26/08/2026 contra julio, daba
    4.090.093 cuando la foto guardada —y los cinco meses de la serie— dicen
    7.593.520. Stock, químicos y retiros daban idénticos; se desvía la cartera y
    arrastra patrimonio (−5,88 M) y utilidad (−5,67 M).

    Con ese Δ en pantalla y una nota que invita a rehacer la foto, alguien puede
    romper un cierre bueno con un número inventado. El día 1 —cuando uno abre
    esto justamente para ver si el cierre salió bien— es el primer día en que el
    número miente.
    """
    import calendar as _cal
    return hoy.day == _cal.monthrange(hoy.year, hoy.month)[1]


@bp.route("/simulacro-cierre", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def simulacro_cierre():
    """SIMULACRO de fin de mes — SOLO LECTURA, no escribe nada.

    Pone el reloj en una fecha simulada (default 01/08) y corre el código REAL:
    (1) el rollover en dry-run → muestra la fila de INICIALES del mes nuevo que
        crearía (apertura = cierre del mes anterior); (2) el HI0/TJ0/PF0 de
        apertura que usaría el balance ese día. Prueba el evento del cierre sobre
        datos reales sin tocar producción. Uso: /admin/health/simulacro-cierre?fecha=2026-08-01
    """
    from datetime import date as _date

    from filters import reset_today_override, set_today_override, today_ec
    from modules.informes.queries import (
        rollover_y_writeback_iniciales,
        tarifa_iniciales_mes_anterior,
    )

    fstr = request.args.get("fecha", "2026-08-01")
    try:
        yy, mm, dd = (int(x) for x in fstr.split("-"))
        fsim = _date(yy, mm, dd)
    except Exception:
        return jsonify({"ok": False, "error": f"fecha inválida: {fstr} (usar YYYY-MM-DD)"})

    token = set_today_override(fsim)
    try:
        visto = str(today_ec())
        roll = rollover_y_writeback_iniciales(dry_run=True)
        # Apertura que usaría el balance ese día (mes calendario anterior)
        hi0 = tarifa_iniciales_mes_anterior(mm, yy, "hilado")
        tj0 = tarifa_iniciales_mes_anterior(mm, yy, "tejido")
        pf0 = tarifa_iniciales_mes_anterior(mm, yy, "terminado")
        vq0 = tarifa_iniciales_mes_anterior(mm, yy, "vq")
        um0 = tarifa_iniciales_mes_anterior(mm, yy, "um")
    finally:
        reset_today_override(token)

    # ------------------------------------------------------------------
    # TMT 2026-08-01 — DRY-RUN DE LA FOTO DE CIERRE.
    # Corre FUERA del override del reloj, a propósito: lo que se quiere ver
    # es qué guardaría la foto **ahora**, con el balance vivo, no con una
    # fecha inventada. Muestra la fila que escribiría al lado de la que ya
    # está guardada, para mirar el Δ ANTES de pisar nada.
    # ------------------------------------------------------------------
    import calendar as _cal

    from modules.informes.queries import crear_snapshot_historia

    _hoy_real = today_ec()
    _ult_dia_hoy = _cal.monthrange(_hoy_real.year, _hoy_real.month)[1]
    if _hoy_real.day == _ult_dia_hoy:
        _a_cerrar = (_hoy_real.year, _hoy_real.month)   # hoy termina el mes
    elif _hoy_real.month == 1:
        _a_cerrar = (_hoy_real.year - 1, 12)
    else:
        _a_cerrar = (_hoy_real.year, _hoy_real.month - 1)

    # El Δ sólo se muestra cuando dice algo — ver `se_puede_comparar_la_foto`.
    _vivo = se_puede_comparar_la_foto(_hoy_real)
    try:
        foto = crear_snapshot_historia(_a_cerrar[0], _a_cerrar[1], dry_run=True)
    except Exception as e:  # el dry-run nunca debe tumbar el simulacro
        foto = {"error": f"{type(e).__name__}: {e}"}

    guardada = None
    if not foto.get("error"):
        guardada = db.fetch_one(
            """
            SELECT id_historia, fecha, fecha_crea, usuario_crea,
                   patrimonio, usret, usuti
              FROM scintela.historia
             WHERE fecha = %s
             ORDER BY id_historia DESC LIMIT 1
            """,
            (foto.get("fecha_cierre"),),
        )

    _row = (foto.get("row") or {}) if not foto.get("error") else {}
    delta = None
    if guardada and _row and _vivo:
        delta = {
            k: round(float(_row.get(k) or 0) - float(guardada.get(k) or 0), 2)
            for k in ("patrimonio", "usret", "usuti")
        }

    apertura_ok = bool(hi0 and pf0)  # hay stock de apertura (no 0)
    return jsonify({
        "ok": apertura_ok and (roll.get("rollover") or roll.get("writeback")),
        "simulando_fecha": fstr,
        "today_ec_visto_por_el_codigo": visto,
        "rollover_dry_run": roll,
        "iniciales_cerrar_mes_auto_crearia": f"{fsim.year:04d}-{fsim.month:02d}",
        "foto_de_cierre_dry_run": {
            "mes_que_cierra": f"{_a_cerrar[0]:04d}-{_a_cerrar[1]:02d}",
            "guardada_hoy": ({k: str(v) for k, v in guardada.items()}
                             if guardada else None),
            "escribiria": {k: _row.get(k) for k in
                           ("fecha", "patrimonio", "usret", "usuti",
                            "banco", "cart", "deuda", "ustock", "uqui")},
            "delta_vs_guardada": delta,
            "error": foto.get("error"),
            "se_puede_comparar": _vivo,
            "nota": ("`patrimonio` va NETO de retiros (patr − usret), igual que "
                     "el dBase (REPLA PATRIMONIO WITH PATR-URET) y que la foto "
                     "diaria. El Δ es lo que se corrige al rehacer la foto — "
                     "acá no se escribió nada."
                     if _vivo else
                     "SIN Δ: el mes que cierra ya pasó, y por ese camino la "
                     "cartera reconstruida no cierra (26/08/2026: daba 4.090.093 "
                     "contra los 7.593.520 de la foto de julio, que es la que "
                     "sigue la serie). NO rehagas la foto con este número. La "
                     "comparación sirve el último día del mes."),
        },
        "apertura_que_usaria_el_balance": {
            "hilado": hi0, "tejido": tj0, "terminado": pf0, "vq": vq0, "um": um0,
            "nota": "= cierre del mes anterior (mesnum-1). Si es 0, se rompería.",
        },
        "veredicto": (
            "OK — la fila del mes nuevo se crearía con el cierre anterior y el "
            "balance arrancaría de un stock válido."
            if apertura_ok else
            "OJO — la apertura da 0: faltaría el cierre del mes anterior."
        ),
    })


# ---------------------------------------------------------------------------
# Cadena de saldos bancarios: ¿el running `saldo` guardado sigue encadenado?
# ---------------------------------------------------------------------------


def _breaks_cadena(no_banco: int, desde, apertura=None) -> list[dict]:
    """Filas donde el running `saldo` GUARDADO no encadena con la anterior.

    ⭐ TMT 2026-08-04 — UNA SOLA REGLA DE SIGNOS. Esto tenía su propio SQL con
    criterio `ABS` ("el salto vale lo mismo que el importe, no me importa el
    signo") porque se creía que la convención de signos de las filas viejas
    del DBF no era legible. **No era cierto.** Verificado ese día sobre las
    1.333 filas de Pichincha: `documento` predice el signo en 1.326, y las 7
    excepciones son exactamente los 7 quiebres reales — el mismo conjunto que
    devolvía el criterio ABS. Lo que estaba roto era el ORDEN (saldo estampado
    por id, leído por (fecha, id)), no los signos.

    Con criterio firmado además se caza un caso que el ABS deja pasar: la
    fila cuyo saldo se mueve el importe correcto pero **para el lado
    equivocado**. Fuente única: `bank_helpers.contar_quiebres`.

    ⭐ TMT 2026-08-14 — con `apertura`, la PRIMERA fila del banco también se
    mira. Sin ella el chequeo arrancaba en la segunda: un saldo mal en la fila
    más vieja no lo veía ni este panel ni el candado del alta. Quién puede
    pasar una apertura y quién se queda con el hueco abierto lo decide
    `modules.bancos.apertura.apertura_para_candado` — una apertura calculada
    desde las transacciones no puede desmentir a las transacciones.
    """
    import bank_helpers
    return bank_helpers.contar_quiebres(
        no_banco=no_banco, desde_fecha=desde, apertura=apertura)


def _apertura_candado(no_banco: int):
    """`(apertura, motivo)` para chequear la PRIMERA fila. Fail-soft.

    Envoltorio del de `modules.bancos.apertura` — si algo falla, el panel
    sigue midiendo lo de siempre (de la segunda fila para abajo) en vez de
    quedarse sin chequeo. TMT 2026-08-14.
    """
    try:
        from modules.bancos.apertura import apertura_para_candado
        return apertura_para_candado(int(no_banco))
    except Exception as exc:  # noqa: BLE001
        return None, f"no pude leer la apertura: {str(exc)[:80]}"


def _evaluar_cadena(*, no_banco, nombre, stored, signed, breaks, n_nulls, dias,
                    derivado=None, origen=None, columna_running=None,
                    apertura_candado=None, motivo_apertura=""):
    """Arma el stat y las alertas de UN banco. Pura — se testea sin Flask.

    ⭐ TMT 2026-08-05 — `stored` ES EL NÚMERO QUE EL BALANCE PUBLICA, no la
    columna `saldo`. Durante un día este chequeo recibió `saldo_stored` (la
    columna running leída con el filtro `ABS(saldo) > 0.5`) y la rotulaba
    `saldo_usado_por_el_balance`. Dejó de ser cierto el 04/08, cuando
    `saldo_bancos()` pasó a calcular el saldo como APERTURA + suma firmada:
    desde entonces la columna es decoración de la pantalla de banco.

    Caso testigo: DEP.PICH. (90) tiene 2 filas — un ND de 455,89 del 23/06 y
    su reverso NC del 25/06 — cadena PERFECTA y saldo real 0,00. El filtro
    `ABS > 0.5` saltea la última fila por valer cero y devuelve −455,89, así
    que el health alertaba **−455,89 de patrimonio corrido que no existía** y
    mandaba a `/bancos/reencadenar`, que para ese banco es un no-op literal
    ("filas que cambian: 0"). Un ⚠ diario por algo legítimo entrena a ignorar
    el panel entero — y encima este mandaba a apretar un botón inútil.

    Ahora:
      - `stored`           = el saldo que el Balance publica (`b["saldo"]`).
      - `columna_running`  = la columna `saldo` (decoración) — sólo stat.
      - la alerta HIGH salta cuando el Balance publica un número que NO se
        deriva de apertura + movimientos, que es la única forma de que haya
        plata de verdad corrida. Si el banco cae a la escalera vieja
        (`origen != 'derivado'`) el que manda es la columna: ahí sí, el
        drift de la columna ES el drift del Balance.
    """
    def _gap(r):
        # Criterio FIRMADO (ver `_breaks_cadena`). Sobre el break real del
        # 03/08 da 155.193,23 y no 155.187,31: la diferencia son los 2×2,96
        # del propio movimiento (tenía que BAJAR 2,96 y subió 155.190,27).
        # Y 155.193,23 es exactamente el Δ que el re-encadenado terminó
        # aplicando a las 107 filas — el criterio firmado PREDICE la
        # corrección, el ABS no.
        return abs((float(r["saldo"]) - float(r["saldo_prev"]))
                   - float(r.get("sgn") or 0))

    gap_total = round(sum(_gap(r) for r in breaks), 2)
    stat = {
        "no_banco": no_banco,
        "nombre": nombre,
        "saldo_usado_por_el_balance": stored,
        # ⭐ TMT 2026-08-04 — el número que hay que mirar es `saldo_derivado`
        # (APERTURA + suma firmada), no `saldo_signed` (suma firmada sola).
        # `delta_stored_vs_signed` valía la apertura del banco — en Pichincha
        # 2.962.335,77 — todos los días, pasara lo que pasara, y la alerta lo
        # anunciaba como patrimonio corrido. Un ⚠ que siempre está prendido
        # entrena a ignorar el panel.
        "saldo_derivado": derivado,
        "delta_stored_vs_derivado": (
            None if derivado is None else round(stored - derivado, 2)),
        "saldo_signed": signed,
        "apertura_implicita": (
            None if derivado is None else round(derivado - signed, 2)),
        # ⭐ La columna `saldo` de transacciones_bancarias, tal como la lee la
        # pantalla del banco. Desde el 04/08 el Balance NO la usa. Se publica
        # como stat —no como alerta— para que se vea que /bancos/<n> puede
        # estar mostrando otro número, sin prender un ⚠ que nadie va a poder
        # apagar (planchar la columna es una decisión aparte de la dueña).
        "saldo_origen": origen,
        "saldo_columna_running": columna_running,
        "delta_columna_vs_derivado": (
            None if (derivado is None or columna_running is None)
            else round(float(columna_running) - derivado, 2)),
        "n_breaks": len(breaks),
        "gap_total": gap_total,
        # ⭐ TMT 2026-08-14 — ¿la PRIMERA fila de este banco está mirada o no?
        # Va como STAT y no como alerta a propósito: un banco sin apertura
        # afirmada no tiene nada malo HOY, tiene un chequeo de menos. Con el
        # dato a la vista no hace falta un ⚠ diario que nadie puede apagar.
        # [[feedback_el_dato_a_la_vista_mata_al_aviso]]
        "primera_fila_chequeada": apertura_candado is not None,
        "apertura_del_candado": apertura_candado,
        "primera_fila_por_que_no": ("" if apertura_candado is not None
                                    else (motivo_apertura or "")),
        "saldos_null": int(n_nulls or 0),
        "breaks": [{
            "id_transaccion": r.get("id_transaccion"),
            "fecha": str(r.get("fecha")),
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or "")[:60],
            "importe": float(r["importe"] or 0),
            "saldo_prev": float(r["saldo_prev"]),
            "saldo": float(r["saldo"]),
            "gap": round(_gap(r), 2),
            # Con `es_primera` el que lee sabe cuál de los dos problemas es:
            # la primera fila contra la apertura declarada no se arregla
            # re-encadenando, se arregla mirando el extracto.
            "es_primera": bool(r.get("es_primera")),
            "fila_anterior": (r.get("concepto_prev") or "")[:40],
        } for r in breaks[:15]],
    }
    alerts = []
    # El invariante DE VERDAD: el número que el BALANCE publica tiene que
    # valer lo mismo que apertura + suma de los movimientos. Con esto, el
    # 03/08 se cazaba solo en vez de a ojo mirando el listado.
    #
    # Si el banco cae a la escalera vieja (sin apertura guardada), el Balance
    # publica la columna running: ahí el drift de la columna ES plata, y hay
    # que mirarlo aunque `stored` ya venga de esa misma columna.
    if derivado is not None and abs(stored - derivado) > 1.0:
        # El consejo importa: re-encadenar sólo arregla algo si la cadena
        # está partida. Con 0 quiebres el re-encadenado es un no-op y mandar
        # a apretarlo hace perder una tarde (pasó el 05/08 con DEP.PICH.).
        _como = ("Re-encadenar en /bancos/reencadenar."
                 if breaks else
                 "OJO: la cadena no tiene quiebres, así que re-encadenar no "
                 "cambia nada — lo que está mal es la APERTURA guardada del "
                 "banco. Afirmala en /bancos/reencadenar.")
        alerts.append({
            "severity": "high",
            "category": "saldo_no_derivable",
            "msg": (
                f"{nombre}: el saldo que usa el BALANCE ({stored:,.2f}) no "
                f"coincide con apertura + movimientos ({derivado:,.2f}) — "
                f"difieren {stored - derivado:,.2f}. Ese es el patrimonio y "
                f"la utilidad corridos por esa plata. {_como}"
            ),
        })
    if gap_total > 1.0:
        alerts.append({
            "severity": "high",
            "category": "cadena_saldos_rota",
            "msg": (
                f"{nombre}: la cadena del running saldo está partida en "
                f"{len(breaks)} punto(s), gap acumulado {gap_total:,.2f}. "
                f"El BALANCE lee el saldo guardado de la última fila, así que "
                f"el patrimonio y la utilidad están corridos por esa plata — y "
                f"la conciliación va a mostrar esa misma diferencia contra el "
                f"extracto. Revisar /bancos/{no_banco} desde la fecha del "
                f"primer break y recomputar."
            ),
        })
    if n_nulls:
        alerts.append({
            "severity": "medium",
            "category": "saldo_null",
            "msg": (f"{nombre}: {n_nulls} fila(s) con `saldo` NULL en los "
                    f"últimos {dias} días — quedan fuera de la cadena."),
        })
    return stat, alerts


@bp.route("/cadena-saldos", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def cadena_saldos():
    """¿La cadena del running `saldo` de cada banco sigue entera?

    ⭐ POR QUÉ EXISTE (TMT 2026-08-03). `saldo_bancos()` NO suma los
    movimientos: toma el `saldo` running GUARDADO de la fila de mayor
    (fecha, id). O sea el BANCO del balance — y con él el PATRIMONIO y la
    UTILIDAD — es *el running que traiga la última fila del día*. Si la
    cadena se parte, todo eso se corre por esa diferencia y **nada avisa**.

    Pasó: una fila `ND Comisiones e impuestos` de **$2,96** creada por la
    conciliación movió Pichincha **+155.187,31**; la utilidad saltó de
    37.658 a 193.749 en 5 minutos sin que se moviera un peso, y la sesión
    de conciliación #60 marcó esa misma diferencia contra el extracto.
    Se descubrió a ojo, mirando el listado. Este chequeo lo hace visible.

    NO cambia ningún cálculo. Sólo mira y avisa.
    """
    from datetime import timedelta

    from filters import today_ec

    dias = max(1, min(int(request.args.get("dias", 120) or 120), 3650))
    desde = today_ec() - timedelta(days=dias)
    alerts: list[dict] = []
    stats: dict = {"dias": dias, "desde": str(desde), "bancos": []}

    try:
        from modules.informes.queries import saldo_bancos

        for b in saldo_bancos():
            no_banco = int(b["no_banco"])
            nombre = (b.get("nombre") or f"Banco {no_banco}").strip()
            ap_candado, motivo_ap = _apertura_candado(no_banco)
            breaks = _breaks_cadena(no_banco, desde, apertura=ap_candado)
            n_nulls = (db.fetch_one(
                "SELECT COUNT(*) AS n FROM scintela.transacciones_bancarias "
                " WHERE no_banco = %s AND fecha >= %s AND saldo IS NULL",
                (no_banco, desde),
            ) or {}).get("n", 0)
            stat, al = _evaluar_cadena(
                no_banco=no_banco,
                nombre=nombre,
                # ⭐ TMT 2026-08-05 — `b["saldo"]` es lo que el Balance
                # PUBLICA; `b["saldo_stored"]` es la columna running, que
                # desde el 04/08 el Balance ya no mira. Pasar la columna acá
                # hacía que el health denunciara como "patrimonio corrido"
                # una plata que el Balance nunca vio (DEP.PICH., −455,89).
                stored=float(b.get("saldo") or 0),
                columna_running=float(b.get("saldo_stored") or 0),
                origen=b.get("saldo_origen"),
                signed=float(b.get("saldo_signed") or 0),
                derivado=(None if b.get("saldo_derivado") is None
                          else float(b.get("saldo_derivado") or 0)),
                breaks=breaks,
                n_nulls=n_nulls,
                dias=dias,
                apertura_candado=ap_candado,
                motivo_apertura=motivo_ap,
            )
            stat["saldo_origen"] = b.get("saldo_origen")
            stats["bancos"].append(stat)
            alerts.extend(al)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "alerts": [{
            "severity": "high", "category": "error",
            "msg": f"cadena-saldos falló: {str(e)[:200]}",
        }], "stats": stats})

    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


# ---------------------------------------------------------------------------
# Pendientes de conciliación: ¿entró basura al backlog?
# ---------------------------------------------------------------------------


def _evaluar_pendientes(*, no_banco, nombre, filas, saldo_banco):
    """Stat + alertas de UN banco. Pura — se testea sin Flask.

    `filas`: pendientes VIVOS (conciliado_en IS NULL) con
    {id, fecha, concepto, documento, monto, tipo}.
    """
    from modules.conciliacion.hoja_parser import _es_fila_de_resumen

    rotulos, gigantes = [], []
    for f in filas:
        rot = _es_fila_de_resumen(f.get("concepto"), f.get("documento"))
        if rot:
            rotulos.append({
                "id": f.get("id"), "rotulo": rot,
                "monto": float(f.get("monto") or 0),
                "fecha": (str(f.get("fecha")) if f.get("fecha") else None),
            })
            continue
        # Ningún movimiento pendiente puede ser más grande que la cuenta
        # entera. Refuerzo del anterior, con otro criterio.
        if saldo_banco and float(f.get("monto") or 0) > abs(float(saldo_banco)):
            gigantes.append({
                "id": f.get("id"),
                "concepto": (f.get("concepto") or f.get("documento") or "")[:60],
                "monto": float(f.get("monto") or 0),
            })

    stat = {
        "no_banco": no_banco, "nombre": nombre,
        "n_pendientes": len(filas),
        "saldo_banco": float(saldo_banco or 0),
        "n_rotulos_de_resumen": len(rotulos),
        "monto_rotulos": round(sum(r["monto"] for r in rotulos), 2),
        "rotulos_de_resumen": rotulos[:20],
        "n_mayores_al_saldo": len(gigantes),
        "mayores_al_saldo": gigantes[:10],
        # Informativo: los "sin fecha" son LEGÍTIMOS (pedido de la dueña
        # 2026-06-04, "que prevalezcan aunque no tengan fecha"). Se listan
        # para poder mirarlos, NO se alerta por ellos.
        "n_sin_fecha": sum(1 for f in filas if not f.get("fecha")),
    }
    alerts = []
    if rotulos:
        alerts.append({
            "severity": "high",
            "category": "resumen_como_pendiente",
            "msg": (
                f"{nombre}: {len(rotulos)} pendiente(s) por "
                f"{stat['monto_rotulos']:,.2f} que NO son movimientos — son "
                f"líneas del RESUMEN contable del propio Excel de pendientes "
                f"(" + ", ".join(r["rotulo"] for r in rotulos[:4]) + "). "
                "Entran al subir con «Hacer prevalecer» un export sin la hoja "
                "RESUMEN separada. Borralos con la ✕ en /conciliacion/banco-v2."
            ),
        })
    if gigantes:
        alerts.append({
            "severity": "high",
            "category": "pendiente_mayor_al_saldo",
            "msg": (
                f"{nombre}: {len(gigantes)} pendiente(s) más grandes que el "
                f"saldo del banco ({float(saldo_banco or 0):,.2f}). Un "
                f"movimiento no puede ser mayor que la cuenta entera."
            ),
        })
    return stat, alerts


# Día en que el depósito pasó a escribir `fechaout` en vez de pisar
# `fechaing` (SHA del fix del 05/08/2026). Las filas anteriores tienen la
# fecha de depósito en `fechaing` y están bien: no se vigilan ni se migran.
_CORTE_FECHAOUT = _dt_date(2026, 8, 5)


@bp.route("/pendientes-conciliacion", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def pendientes_conciliacion():
    """¿Hay basura en el backlog de pendientes del banco?

    ⭐ POR QUÉ (TMT 2026-08-03). El Excel de pendientes que genera la app
    llevaba el RESUMEN contable al pie de la misma hoja, con el rótulo en la
    columna CODIGO — que es de donde «Hacer prevalecer» lee el DOCUMENTO.
    Bajar ese archivo y volver a subirlo (el ciclo normal de trabajo) cargaba
    las 6 líneas del resumen como pendientes: **$8.304.132,19** de créditos
    fantasma, y el export siguiente salió con DIFERENCIA −8.323.357,19.

    Ya está arreglado de raíz (el resumen vive en su propia hoja) y hay red en
    el parser, pero los Excel viejos siguen dando vueltas. Esto lo mira todos
    los días. NO cambia ningún cálculo.
    """
    alerts: list[dict] = []
    stats: dict = {"bancos": []}
    try:
        from modules.informes.queries import saldo_bancos

        saldos = {int(b["no_banco"]): float(b.get("saldo_stored") or 0)
                  for b in saldo_bancos()}
        filas = db.fetch_all(
            """
            SELECT h.id, h.no_banco, h.fecha, h.concepto, h.documento,
                   h.monto, h.tipo,
                   COALESCE(b.nombre, '') AS nombre
              FROM scintela.banco_historicos_pendientes h
              LEFT JOIN scintela.banco b ON b.no_banco = h.no_banco
             WHERE h.conciliado_en IS NULL
             ORDER BY h.no_banco, h.fecha NULLS FIRST
            """
        ) or []
        por_banco: dict = {}
        for f in filas:
            por_banco.setdefault(int(f["no_banco"]), []).append(f)
        for no_banco, fs in sorted(por_banco.items()):
            nombre = (fs[0].get("nombre") or f"Banco {no_banco}").strip()
            stat, al = _evaluar_pendientes(
                no_banco=no_banco, nombre=nombre, filas=fs,
                saldo_banco=saldos.get(no_banco),
            )
            stats["bancos"].append(stat)
            alerts.extend(al)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "alerts": [{
            "severity": "high", "category": "error",
            "msg": f"pendientes-conciliacion falló: {str(e)[:200]}",
        }], "stats": stats})

    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


# ---------------------------------------------------------------------------
# Saldo DERIVADO: ¿se puede dejar de leer la cadena y pasar a sumar?
# ---------------------------------------------------------------------------


def _derivar_cadena(filas: list[dict]) -> dict:
    """Deriva el importe FIRMADO de cada fila a partir del saldo guardado.

    ⭐ La identidad que hace que esto no mueva NADA, por álgebra:

        firmado[0] = saldo[0]
        firmado[n] = saldo[n] − saldo[n−1]        (n > 0)

        ⇒ SUM(firmado[0..k]) = saldo[k]           (telescópica)

    O sea: sumar los firmados reproduce EXACTAMENTE el saldo guardado, en
    cualquier punto de la cadena. No adivina el signo desde el `documento`
    —que es de donde salen todos los problemas—: lo LEE de la cadena que ya
    está escrita.

    Y de yapa sale gratis el diagnóstico: una fila donde
    `|firmado| ≠ |importe|` es, por definición, un quiebre de la cadena.
    Las demás son derivables sin ambigüedad.
    """
    filas = [f for f in filas if f.get("saldo") is not None]
    derivables, quiebres = 0, []
    prev = None
    for f in filas:
        saldo = float(f["saldo"])
        firmado = saldo if prev is None else round(saldo - prev, 2)
        imp = abs(float(f.get("importe") or 0))
        if prev is not None and abs(abs(firmado) - imp) > 0.02:
            quiebres.append({
                "id_transaccion": f.get("id_transaccion"),
                "fecha": str(f.get("fecha")),
                "documento": (f.get("documento") or "").strip(),
                "concepto": (f.get("concepto") or "")[:50],
                "importe": imp,
                "firmado_derivado": firmado,
                "gap": round(abs(abs(firmado) - imp), 2),
            })
        elif prev is not None:
            derivables += 1
        prev = saldo
    return {
        "n_filas": len(filas),
        "n_derivables": derivables,
        "n_quiebres": len(quiebres),
        "quiebres": quiebres[:20],
        "suma_derivada": round(prev, 2) if prev is not None else 0.0,
    }


@bp.route("/saldo-derivado", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def saldo_derivado():
    """DRY-RUN: ¿pasar de LEER la cadena a SUMAR movimientos mueve algún número?

    ⭐ EL PROBLEMA DE FONDO (TMT 2026-08-03). El saldo corrido está GUARDADO y
    se usa como fuente de verdad, cuando es un valor DERIVADO. Y no se puede
    re-derivar porque `transacciones_bancarias.importe` **no lleva su signo**:
    hay que adivinarlo desde el `documento`, con reglas que cambiaron entre el
    dBase y la web (`bank_helpers._signed_delta`).

    De ahí sale todo: un recompute total es peligroso (el dry-run del 29/06
    daba −472.943), un solo insert backdated corrompe historia, y quedan 7
    filas de junio-julio que no se pueden arreglar sin el extracto.

    El arreglo es que el importe lleve signo y el saldo salga de un `SUM()`:
    **sin cadena no hay cadena que romper, y una suma no depende del orden**,
    así que un insert backdated deja de poder corromper nada.

    Esta pantalla NO ESCRIBE. Sólo demuestra la premisa: que derivar el signo
    de la cadena reproduce el saldo actual **al centavo**, banco por banco.
    Si algún Δ no es 0,00, la migración no se hace.
    """
    alerts: list[dict] = []
    stats: dict = {"bancos": [], "delta_maximo": 0.0}
    try:
        from modules.informes.queries import saldo_bancos

        for b in saldo_bancos():
            no_banco = int(b["no_banco"])
            nombre = (b.get("nombre") or f"Banco {no_banco}").strip()
            stored = float(b.get("saldo_stored") or 0)
            if not stored and not int(b.get("n_transacciones") or 0):
                continue
            filas = db.fetch_all(
                """
                SELECT id_transaccion, fecha, documento, concepto,
                       importe, saldo
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                   AND fecha <= CURRENT_DATE
                 ORDER BY fecha, id_transaccion
                """,
                (no_banco,),
            ) or []
            n_null = sum(1 for f in filas if f.get("saldo") is None)
            d = _derivar_cadena(filas)
            # `saldo_bancos()` toma el último saldo NO-CERO; la suma derivada
            # termina en el último saldo a secas. Comparamos contra ese mismo.
            delta = round(d["suma_derivada"] - stored, 2)
            stats["bancos"].append({
                "no_banco": no_banco, "nombre": nombre,
                "saldo_hoy_que_usa_el_balance": stored,
                "saldo_si_sumaramos": d["suma_derivada"],
                "delta": delta,
                "n_filas": d["n_filas"],
                "n_derivables": d["n_derivables"],
                "n_quiebres": d["n_quiebres"],
                "saldos_null_no_derivables": n_null,
                "quiebres": d["quiebres"],
            })
            stats["delta_maximo"] = max(stats["delta_maximo"], abs(delta))
            if abs(delta) > 0.01:
                alerts.append({
                    "severity": "high", "category": "derivado_no_reproduce",
                    "msg": (f"{nombre}: sumar los importes firmados da "
                            f"{d['suma_derivada']:,.2f} y el balance usa "
                            f"{stored:,.2f} (Δ {delta:+,.2f}). Con este Δ la "
                            f"migración NO se hace."),
                })
            if n_null:
                alerts.append({
                    "severity": "medium", "category": "saldo_null",
                    "msg": (f"{nombre}: {n_null} fila(s) con `saldo` NULL — no "
                            f"se les puede derivar el signo desde la cadena."),
                })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "alerts": [{
            "severity": "high", "category": "error",
            "msg": f"saldo-derivado falló: {str(e)[:200]}",
        }], "stats": stats})

    stats["veredicto"] = (
        "OK — derivar el signo de la cadena reproduce el saldo de cada banco "
        "al centavo. La migración a `importe_firmado` + SUM() no movería "
        "ningún número."
        if not alerts else
        "OJO — hay bancos donde la suma NO reproduce el saldo. No migrar."
    )
    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


@bp.route("/deposito-sin-fechaout", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def deposito_sin_fechaout():
    """¿Algún camino de depósito volvió a olvidarse de escribir `fechaout`?

    ⭐ POR QUÉ (TMT 2026-08-05). `scintela.cheque.fechaing` significaba DOS
    cosas a la vez: en las filas del dBase es FECHING = el día que el cheque
    ENTRÓ a cartera, y las dos rutas de depósito de PC escribían ahí la fecha
    de SALIDA. Depositar un cheque viejo le borraba el día en que entró, y el
    resumen de cobranza —que agrupa por día de ingreso— lo imprimía como
    cobranza del día del depósito. La hoja del 04/08 que va a contabilidad
    salió con 46 cheques fantasma por $74.165,81, todos "sin aplicar a
    facturas". 459 filas afectadas desde el 13/07.

    Desde el 05/08 el depósito escribe `fechaout`, igual que las otras once
    salidas de cartera (C, 9, X, E, T y sus deshacer). Esto vigila que siga
    siendo así: **un cheque con un movimiento bancario 'DE' posterior al
    corte tiene que tener `fechaout`**. Si mañana aparece una ruta nueva de
    depósito que se olvida, salta acá al día siguiente en vez de aparecer en
    una hoja impresa dos semanas después.

    No mira las filas viejas a propósito: las ~1.200 depositadas antes del
    corte tienen la fecha en `fechaing` y ahí ese valor ES la fecha de
    depósito — están bien y no se migran. Un ⚠ diario por algo legítimo
    entrena a ignorar el panel.

    Solo lectura. NO cambia ningún cálculo.
    """
    alerts, stats = _evaluar_fechaout(
        # (a) EL SÍNTOMA CLÁSICO: depositado, con su movimiento bancario 'DE',
        # y sin fecha de salida. Es lo que cazó las dos rutas del 05/08.
        # ⚠ El COUNT sale de su propia consulta: antes se contaban las filas
        # YA recortadas por el LIMIT y `n_sin_fechaout` se clavaba en 50 —
        # el 10/08 dijo 50 cuando eran 104. Un detector que subreporta hace
        # que el arreglo parezca más chico de lo que es.
        n_con_mov=int((db.fetch_one(
            """
            SELECT COUNT(DISTINCT c.id_cheque) AS n
              FROM scintela.cheque c
              JOIN scintela.chequextransaccion cxt ON cxt.id_cheque = c.id_cheque
              JOIN scintela.transacciones_bancarias tb
                ON tb.id_transaccion = cxt.id_transaccion
             WHERE UPPER(COALESCE(tb.documento, '')) = 'DE'
               AND tb.fecha >= %s
               AND UPPER(COALESCE(c.stat, '')) IN ('B', 'A', 'V')
               AND c.fechaout IS NULL
            """,
            (_CORTE_FECHAOUT,),
        ) or {}).get("n") or 0),
        filas_con_mov=db.fetch_all(
            """
            SELECT c.id_cheque, c.no_cheque, c.codigo_cli, c.importe, c.stat,
                   c.no_banco, COALESCE(c.usuario_crea, '') AS usuario_crea,
                   MIN(tb.fecha)::text AS fecha_deposito
              FROM scintela.cheque c
              JOIN scintela.chequextransaccion cxt ON cxt.id_cheque = c.id_cheque
              JOIN scintela.transacciones_bancarias tb
                ON tb.id_transaccion = cxt.id_transaccion
             WHERE UPPER(COALESCE(tb.documento, '')) = 'DE'
               AND tb.fecha >= %s
               AND UPPER(COALESCE(c.stat, '')) IN ('B', 'A', 'V')
               AND c.fechaout IS NULL
             GROUP BY c.id_cheque, c.no_cheque, c.codigo_cli, c.importe,
                      c.stat, c.no_banco, c.usuario_crea
             ORDER BY 8 DESC, c.id_cheque
             LIMIT 20
            """,
            (_CORTE_FECHAOUT,),
        ) or [],
        # (b) EL INVARIANTE, sin depender del movimiento bancario. Un cheque
        # que NACE fuera de cartera salió el día que entró y tiene que llevar
        # `fechaout`.
        #
        # TMT 2026-08-10 (2a pasada). Acá había un `usuario_modifica IS NULL`.
        # Servía para MEDIR —probaba que ninguna otra ruta había tocado esas
        # filas— pero como criterio del vigía pregunta por el CAMINO, que es el
        # error que arrastra todo este tema: una fila a la que alguien le editó
        # el concepto por cualquier motivo se volvía invisible AUNQUE siguiera
        # fuera de cartera y sin fecha de salida. La pregunta es sobre el
        # ESTADO. Medido el 10/08: da lo mismo con y sin el filtro (117 y 117
        # antes de la mig 0185, 0 y 0 después), así que sacarlo no enciende
        # nada — sólo cierra la puerta de atrás. La rama (a) no ve el efectivo (99 → 'C'): va a CAJA, no
        # genera un 'DE', y por eso 13 cobros en efectivo estuvieron sin
        # NINGUNA de las dos fechas sin que nadie se enterara. Este criterio
        # no le pregunta a la plata por dónde salió.
        n_nace_afuera=int((db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.cheque
             WHERE fecha_crea::date >= %s
               AND UPPER(COALESCE(stat, '')) NOT IN ('Z','P','D','1','2','3')
               AND fechaout IS NULL
            """,
            (_CORTE_FECHAOUT,),
        ) or {}).get("n") or 0),
        filas_nace_afuera=db.fetch_all(
            """
            SELECT id_cheque, no_cheque, codigo_cli, importe, stat, no_banco,
                   COALESCE(usuario_crea, '') AS usuario_crea,
                   fecha_crea::date::text AS nacio
              FROM scintela.cheque
             WHERE fecha_crea::date >= %s
               AND UPPER(COALESCE(stat, '')) NOT IN ('Z','P','D','1','2','3')
               AND fechaout IS NULL
             ORDER BY fecha_crea DESC, id_cheque
             LIMIT 20
            """,
            (_CORTE_FECHAOUT,),
        ) or [],
    )
    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


def _evaluar_fechaout(*, n_con_mov, filas_con_mov, n_nace_afuera,
                      filas_nace_afuera) -> tuple[list[dict], dict]:
    """Parte pura — sin base, para poder testear las dos ramas."""
    alerts: list[dict] = []
    if n_con_mov:
        alerts.append({
            "nivel": "HIGH",
            "que": f"{n_con_mov} cheque(s) depositados desde el "
                   f"{_CORTE_FECHAOUT:%d/%m/%Y} sin fecha de salida de cartera",
            "por_que": "alguna ruta de depósito está escribiendo la fecha en "
                       "`fechaing` (el día de INGRESO) en vez de `fechaout`. "
                       "Eso los convierte en cobranza del día del depósito en "
                       "/cheques/resumen-dia.",
            "donde_mirar": sorted({
                f"banco {f.get('no_banco')} · {f.get('usuario_crea') or '?'}"
                for f in filas_con_mov
            }),
            "filas": filas_con_mov,
        })
    if n_nace_afuera:
        alerts.append({
            "nivel": "HIGH",
            "que": f"{n_nace_afuera} cheque(s) NACIERON fuera de cartera desde "
                   f"el {_CORTE_FECHAOUT:%d/%m/%Y} y no tienen fecha de salida",
            "por_que": "un cheque que se crea ya depositado (90/91 → 'B') o ya "
                       "cobrado en caja (99 → 'C') entró y salió el mismo día: "
                       "le falta `fechaout`. Mirar el INSERT de "
                       "`cheques.queries.crear()`, no las rutas de depósito.",
            "donde_mirar": sorted({
                f"banco {f.get('no_banco')} · stat {f.get('stat')}"
                for f in filas_nace_afuera
            }),
            "filas": filas_nace_afuera,
        })
    return alerts, {
        "corte": _CORTE_FECHAOUT.isoformat(),
        "n_sin_fechaout": n_con_mov,
        "n_nacidos_fuera_de_cartera_sin_fechaout": n_nace_afuera,
    }


# ---------------------------------------------------------------------------
# Un cheque devuelto tiene que dejar su nota de debito en el banco
# ---------------------------------------------------------------------------
#
# TMT 2026-08-27 (el caso GUG): Alex marco devuelto un cheque de GUG de
# $1.000 y los libros no se movieron — la devolucion de un cheque del dBase
# (sin deposito vinculado en PC) no genera la ND, y el banco SI habia
# debitado la plata. La conciliacion quedo $1.000 arriba y el agujero se
# encontro a mano dias despues. Encima el mismo cliente tenia DOS cheques
# devueltos del mismo importe: un "¿existe una ND?" no alcanza — hay que
# CONTAR devoluciones contra NDs por cliente e importe.


@bp.route("/devuelto-sin-nd", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def devuelto_sin_nd():
    """¿Alguna devolucion de cheque quedo sin su nota de debito en libros?

    Cuenta, por cliente e importe, los cheques marcados devueltos desde el
    corte (que siguen devueltos) y las ND de devolucion del mismo cliente e
    importe desde el corte. Si hay mas devoluciones que NDs, falta plata:
    el banco debito el rebote y los libros no.

    Solo lectura. NO cambia ningun calculo.
    """
    grupos = db.fetch_all(
        """
        SELECT UPPER(TRIM(COALESCE(c.codigo_cli, ''))) AS codigo_cli,
               c.importe,
               COUNT(DISTINCT c.id_cheque) AS n_devueltos,
               ARRAY_AGG(DISTINCT c.id_cheque) AS ids
          FROM scintela.cheque c
          JOIN scintela.mov_doble m
            ON m.origen_table = 'cheque' AND m.origen_id = c.id_cheque
           AND m.tipo = 'cheque_devuelto' AND m.estado = 'activo'
         WHERE m.fecha_creacion >= %s
           AND UPPER(TRIM(COALESCE(c.stat, ''))) IN ('1', '2', '9')
           -- TMT 2026-08-27: solo cuenta si el cheque venia de estar
           -- DEPOSITADO (B/A/V) o si el protesto no anoto de donde salio.
           -- Un D->1 (re-clasificar un devuelto del dBase, caso MTV) o un
           -- Z->1 (rebota antes de depositarse) no movieron el banco y no
           -- les corresponde ND: contarlos es un ⚠ diario por algo
           -- legitimo, que entrena a ignorar el panel.
           AND COALESCE(m.metadata->>'stat_prev', '')
               NOT IN ('D', '1', '2', '9', 'R', 'Z')
         GROUP BY 1, 2
        """,
        (_CORTE_FECHAOUT,),
    ) or []
    filas = []
    for g in grupos:
        cli = (g.get("codigo_cli") or "").strip().upper()
        imp = float(g.get("importe") or 0)
        ids = [int(x) for x in (g.get("ids") or [])]
        n_nds = int((db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.transacciones_bancarias tb
             WHERE UPPER(TRIM(COALESCE(tb.documento, ''))) = 'ND'
               AND tb.fecha >= %s
               AND ABS(COALESCE(tb.importe, 0) - %s) <= 0.01
               AND ( tb.numreferencia = ANY(%s)
                     OR UPPER(TRIM(COALESCE(tb.prov, ''))) = %s
                     OR UPPER(COALESCE(tb.concepto, '')) LIKE %s )
            """,
            (_CORTE_FECHAOUT, imp, ids or [0], cli, f"%{cli}%"),
        ) or {}).get("n") or 0)
        filas.append({
            "codigo_cli": cli,
            "importe": imp,
            "n_devueltos": int(g.get("n_devueltos") or 0),
            "n_nds": n_nds,
            "ids_cheques": ids,
        })
    alerts, stats = _evaluar_devuelto_sin_nd(filas)
    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


def _evaluar_devuelto_sin_nd(filas: list[dict]) -> tuple[list[dict], dict]:
    """Parte pura — sin base, para poder testear el conteo.

    La regla es POR CANTIDAD, no por existencia: dos cheques devueltos del
    mismo cliente por el mismo importe necesitan DOS notas de debito. Con un
    "¿existe alguna?" el segundo rebote de GUG se escondia detras de la ND
    del primero.
    """
    alerts: list[dict] = []
    total_faltantes = 0
    for f in filas:
        faltan = int(f.get("n_devueltos") or 0) - int(f.get("n_nds") or 0)
        if faltan <= 0:
            continue
        total_faltantes += faltan
        alerts.append({
            "nivel": "HIGH",
            "que": (f"{f.get('codigo_cli')}: {f.get('n_devueltos')} cheque(s) "
                    f"devueltos de {f.get('importe'):.2f} y solo "
                    f"{f.get('n_nds')} nota(s) de debito en el banco"),
            "por_que": "el banco debito el rebote y los libros no: falta "
                       "cargar la ND de la devolucion (los cheques del dBase "
                       "sin deposito vinculado no la generan solos). Mientras "
                       "falte, el banco del programa queda arriba del real "
                       "por ese importe.",
            "donde_mirar": [f"/cheques/{i}" for i in
                            (f.get("ids_cheques") or [])],
        })
    return alerts, {
        "corte": _CORTE_FECHAOUT.isoformat(),
        "n_grupos_devueltos": len(filas),
        "n_nds_faltantes": total_faltantes,
    }



# ---------------------------------------------------------------------------
# Un espejo de anticipo no puede quedar vivo sin su padre
# ---------------------------------------------------------------------------
#
# TMT 2026-08-19 (dueña, mirando /informes/traza: *"me suena raro que el espejo
# no sea + y -"*). Un anticipo son DOS filas: el cheque con la plata y un
# cheque-espejo NB=98 negativo, que es el saldo a favor del cliente. Si el
# padre se anula y el espejo queda vivo, el negativo no tiene contrapartida:
# el cliente figura con plata a favor que nadie le debe y la utilidad baja por
# ese importe, de una sola punta.
#
# Pasó tres veces sin que nadie se enterara —HOM $2.626,27 (19/08), ADI $500
# (21/07) y FES $2.223,96 (11/08)— porque la anulación no se llevaba al
# espejo. Las dos rutas ya cascadean; esto vigila el ESTADO, no el camino: si
# mañana aparece una tercera forma de cerrar un cheque, salta al día siguiente
# en vez de descubrirse mirando un renglón raro un mes después.
#
# Los espejos SIN padre (72 filas de 2022 a julio 2026) no entran: son los
# saldos a favor que vinieron del dBase sin el vínculo, y son deuda real.


def _evaluar_espejos_huerfanos(filas: list[dict]) -> tuple[list[dict], dict]:
    """Parte pura — sin base, para poder testear el aviso."""
    alerts: list[dict] = []
    total = round(sum(abs(float(f.get("importe") or 0)) for f in filas), 2)
    if filas:
        alerts.append({
            "nivel": "HIGH",
            "que": (f"{len(filas)} espejo(s) de anticipo vivos con el cheque "
                    f"padre anulado — ${total:,.2f} de utilidad de una sola punta"),
            "por_que": "el espejo NB=98 es la contrapartida del saldo a favor. "
                       "Sin su padre no compensa nada: el cliente figura con "
                       "plata a favor que no existe. Se anula por "
                       "/cheques/<id>/anular-error-carga.",
            "donde_mirar": sorted({str(f.get("codigo_cli") or "?") for f in filas}),
            "filas": filas,
        })
    return alerts, {"n_espejos_huerfanos": len(filas), "total_us": total}


@bp.route("/espejo-huerfano", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def espejo_huerfano():
    """¿Quedó algún espejo de anticipo vivo con el padre muerto?"""
    filas = db.fetch_all(
        """
        SELECT e.id_cheque, e.fecha::text AS fecha, e.codigo_cli, e.importe,
               e.stat, e.id_cheque_padre, p.stat AS stat_padre,
               COALESCE(e.usuario_crea, '') AS usuario_crea
          FROM scintela.cheque e
          JOIN scintela.cheque p ON p.id_cheque = e.id_cheque_padre
         WHERE e.no_banco IN (97, 98)
           AND COALESCE(e.importe, 0) < 0
           AND TRIM(COALESCE(e.stat, '')) IN ('Z','P','D','1','2','3')
           AND TRIM(COALESCE(p.stat, '')) IN ('X','T','R')
         ORDER BY e.fecha, e.id_cheque
         LIMIT 50
        """,
    ) or []
    alerts, stats = _evaluar_espejos_huerfanos(filas)
    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})


# Endpoint combinado: /admin/health/all (para un único curl del cron)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# El $/kg del hilado tiene que poder reconstruirse
# ---------------------------------------------------------------------------
#
# El 07/08/2026 el hilado quedó valuado a 3,0717 US$/kg cuando correspondía
# 3,0387: la APERTURA del mes salía de `stock_act_ukg` —una tarifa de CIERRE,
# que ya tiene las compras del mes adentro— y el promedio ponderado se las
# volvía a diluir encima. Los mismos $504.819 contados dos veces.
#
# Nadie se enteró durante OCHO DÍAS. El síntoma estuvo a la vista desde el
# 31/07 a las 17:56 —la utilidad saltaba sin que se movieran los kilos— y no
# había nada mirándolo.
#
# Este check no busca ESE bug: busca el SÍNTOMA, que es el mismo venga por
# donde venga. El $/kg del hilado tiene que ser reconstruible con la
# aritmética del promedio ponderado y nada más:
#
#     esperado = ((hilado_kg - compras_kg) * apertura + compras_us) / hilado_kg
#
# donde `apertura` es el cierre GRABADO del mes anterior (queries.
# apertura_ukg_hilado). Si el $/kg que se muestra no se reconstruye así, algo
# lo está moviendo por fuera del promedio ponderado.

# Tolerancia EN DÓLARES sobre el hilado, no en $/kg: un centavo no dice nada y
# lo que importa es cuánta plata mueve. Medido el 07/08 con el cálculo ya
# corregido, la diferencia normal es ~$1.600 — viene de que los kilos "en
# máquinas" se valúan a la apertura y no al promedio, que es la convención de
# la app. El bug daba ~$63.700. El umbral queda lejos de los dos: ni un ⚠
# diario por ruido, ni un agujero que pase. Ver [[feedback_flujo_chequeo_coherencia]].
_HILADO_UKG_TOL_US = 10000.0


#: Cuántos minutos puede tener la última foto antes de encender la luz. El
#: intervalo es de 5 min y en la práctica sale una cada ~6; 20 deja pasar un
#: deploy (la app reinicia y el hilo arranca de nuevo) sin encender nada.
_TRAZA_FRESCA_MIN = 20


@bp.route("/traza-fresca", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def traza_fresca():
    """¿Hay foto nueva de la traza, o la grabadora dejó de guardar?

    🚨 TMT 2026-08-10: *"y también algo que avise si no está guardando"*. Ese
    día la foto empezó a insertar una columna que no existía y la grabadora
    —fail-soft por diseño— dejó de guardar EN SILENCIO durante diez minutos.
    Hubo un aviso en la campanita recién cuando se agregó; esto es el otro
    lado: no pregunta "¿explotó?" sino **"¿hay foto nueva?"**, así que caza
    cualquier motivo —el hilo de fondo muerto, el lock trabado, el proceso
    caído— y no sólo la excepción que alguien pensó en atrapar.
    """
    alerts: list = []
    fila = db.fetch_one(
        "SELECT creado_en, EXTRACT(EPOCH FROM (now() - creado_en)) / 60 AS min "
        "FROM scintela.traza_utilidad ORDER BY creado_en DESC, id_traza DESC LIMIT 1")
    if not fila:
        return jsonify({"ok": True, "alerts": [{
            "severity": "low", "category": "sin_traza",
            "msg": "Todavía no hay ninguna foto guardada."}], "stats": {}})
    edad = round(float(fila.get("min") or 0), 1)
    ok = edad <= _TRAZA_FRESCA_MIN
    if not ok:
        alerts.append({
            "severity": "high", "category": "traza_congelada",
            "msg": (f"La última foto de la traza es de hace {edad:.0f} minutos "
                    f"(el tope son {_TRAZA_FRESCA_MIN}). La grabadora no está "
                    f"guardando.")})
    return jsonify({"ok": ok, "alerts": alerts,
                    "stats": {"ultima": str(fila.get("creado_en")),
                              "edad_min": edad,
                              "tope_min": _TRAZA_FRESCA_MIN}})


@bp.route("/hilado-ukg", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def hilado_ukg_reconstruible():
    """¿El $/kg del hilado se explica con la apertura + las compras del mes?"""
    alerts: list = []
    stats: dict = {}

    fila = db.fetch_one(
        """
        SELECT creado_en, hilado_kg, hilado_ukg, compras_kg, compras_us,
               kg_sin_costo
          FROM scintela.traza_utilidad
         ORDER BY creado_en DESC, id_traza DESC
         LIMIT 1
        """
    )
    if not fila:
        alerts.append({
            "severity": "low",
            "category": "sin_traza",
            "msg": "No hay fotos en scintela.traza_utilidad — no se puede chequear.",
        })
        return jsonify({"ok": True, "alerts": alerts, "stats": stats})

    hil_kg = float(fila.get("hilado_kg") or 0)
    ukg_real = float(fila.get("hilado_ukg") or 0)
    com_kg = float(fila.get("compras_kg") or 0)
    com_us = float(fila.get("compras_us") or 0)

    hoy = _dt_date.today()
    try:
        from filters import today_ec
        hoy = today_ec()
    except Exception:  # noqa: BLE001 -- fail-soft
        pass

    try:
        from modules.informes.queries import apertura_ukg_hilado
        apertura = float(apertura_ukg_hilado(hoy.month, hoy.year) or 0)
    except Exception as e:  # noqa: BLE001 -- fail-soft
        alerts.append({
            "severity": "low",
            "category": "sin_apertura",
            "msg": f"No se pudo leer la apertura del mes: {e}",
        })
        return jsonify({"ok": True, "alerts": alerts, "stats": stats})

    stats.update({
        "foto": str(fila.get("creado_en")),
        "apertura_ukg": round(apertura, 6),
        "hilado_kg": round(hil_kg, 2),
        "compras_kg": round(com_kg, 2),
        "compras_us": round(com_us, 2),
        "kg_sin_costo": round(float(fila.get("kg_sin_costo") or 0), 2),
        "ukg_mostrado": round(ukg_real, 6),
    })

    # 🚨 El chequeo de abajo compara el $/kg contra el esperado CON ESTAS
    # MISMAS compras: si las compras vienen en cero, el esperado también da la
    # apertura y cierra perfecto. El 12/08 dijo ok:true mientras la utilidad
    # estaba 64.393 abajo por exactamente eso. Lo que hay que mirar es el
    # INSUMO: cero compras del mes mientras el programa tiene hilo comprado
    # significa que Asinfo no contestó, no que no se compró.
    if com_kg <= 0 and com_us <= 0:
        try:
            from modules.asinfo.service import dolares_hilo_del_mes
            us_propios = dolares_hilo_del_mes(hoy.year, hoy.month)
        except Exception:  # noqa: BLE001 -- fail-soft
            us_propios = 0.0
        stats["compras_us_en_el_programa"] = round(us_propios, 2)
        if us_propios > 0:
            alerts.append({
                "severity": "high",
                "category": "compras_del_mes_en_cero",
                "msg": (
                    f"La ultima foto tiene las compras de hilado del mes en "
                    f"CERO, pero en scintela.compra hay {us_propios:,.2f} US$ "
                    f"de hilo comprado este mes: Asinfo no esta contestando el "
                    f"cruce de importaciones. Sin compras el promedio ponderado "
                    f"no se diluye y el $/kg cae al de apertura, revaluando "
                    f"TODO el stock hacia abajo. El balance sostiene la tarifa "
                    f"anterior, pero mientras dure no entra ninguna compra "
                    f"nueva a la valuacion."
                ),
            })

    if apertura <= 0 or hil_kg <= 0:
        alerts.append({
            "severity": "low",
            "category": "sin_datos",
            "msg": ("Falta la apertura del mes anterior o el stock de hilado "
                    "está en cero — no se puede reconstruir."),
        })
        return jsonify({"ok": True, "alerts": alerts, "stats": stats})

    esperado = ((hil_kg - com_kg) * apertura + com_us) / hil_kg
    gap_ukg = ukg_real - esperado
    gap_us = gap_ukg * hil_kg
    stats["ukg_esperado"] = round(esperado, 6)
    stats["gap_ukg"] = round(gap_ukg, 6)
    stats["gap_us"] = round(gap_us, 2)
    stats["tolerancia_us"] = _HILADO_UKG_TOL_US

    if abs(gap_us) > _HILADO_UKG_TOL_US:
        alerts.append({
            "severity": "high",
            "category": "hilado_ukg_no_reconstruible",
            "msg": (
                f"El $/kg del hilado ({ukg_real:.4f}) no se explica con la "
                f"apertura del mes ({apertura:.4f}) mas las compras "
                f"({com_us:,.2f} por {com_kg:,.0f} kg): deberia dar "
                f"{esperado:.4f}. Son {gap_ukg:+.4f} US$/kg = {gap_us:+,.2f} "
                f"US$ sobre el hilado, y como el $/kg del hilado arrastra "
                f"tejido (+0,5) y terminado (+2,2) el efecto sobre el "
                f"patrimonio es mayor. Empezar por la APERTURA: tiene que ser "
                f"el cierre del mes anterior y no puede recalcularse durante "
                f"el mes."
            ),
        })

    return jsonify({
        "ok": all(a["severity"] == "low" for a in alerts),
        "alerts": alerts,
        "stats": stats,
    })


# ---------------------------------------------------------------------------
# El permiso que existe en el repo y no en la base
# ---------------------------------------------------------------------------
#
# `config/roles.py` se declara a sí mismo "the single source of truth", pero el
# que manda en runtime es `seguridad.permiso`. Cambiar el archivo sin migración
# deja producción EXACTAMENTE IGUAL y los tests en verde — pasó con las migs
# 0164/0165 y con `cupos.editar`. Un rol al que el repo le da un permiso que la
# base no tiene es un cambio que alguien cree hecho y no está.
#
# Al revés también importa, pero menos: un permiso que la base tiene y el
# archivo no declara es alguien que lo agregó a mano (o una migración que no
# volvió al repo). No apaga nada hoy, así que va como aviso y no como alerta —
# un ⚠ diario por algo que no rompe entrena a ignorar el panel.
#
# ⭐ Este es el único de los tres chequeos de drift que necesita la base VIVA.
# Los otros dos —clases de Tailwind sin compilar y links a pantallas que no
# existen— son estáticos y se atajan en el CI, donde frenan el merge en vez de
# avisar al día siguiente: `tests/test_drift_estatico.py`.


def _evaluar_drift_permisos(codigo: dict, base: dict) -> tuple[list[dict], dict]:
    """Parte pura: dos dicts {rol: set(permisos)} → (alertas, stats)."""
    alerts: list[dict] = []

    roles_faltantes = sorted(set(codigo) - set(base))
    if roles_faltantes:
        alerts.append({
            "nivel": "HIGH",
            "que": f"{len(roles_faltantes)} rol(es) declarados en "
                   f"config/roles.py que NO existen en seguridad.rol",
            "por_que": "el rol vive sólo en el repo: nadie lo puede tener.",
            "filas": roles_faltantes,
        })

    solo_codigo = {
        rol: sorted(codigo[rol] - base.get(rol, set()))
        for rol in sorted(set(codigo) & set(base))
        if codigo[rol] - base.get(rol, set())
    }
    if solo_codigo:
        alerts.append({
            "nivel": "HIGH",
            "que": f"{sum(len(v) for v in solo_codigo.values())} permiso(s) "
                   f"que el repo le da a un rol y la base no",
            "por_que": "el que manda en runtime es `seguridad.permiso`. Un "
                       "cambio en config/roles.py sin migración deja "
                       "producción igual, con los tests en verde.",
            "filas": [{"rol": r, "permisos": p} for r, p in solo_codigo.items()],
        })

    solo_base = {
        rol: sorted(base[rol] - codigo.get(rol, set()))
        for rol in sorted(base)
        if base[rol] - codigo.get(rol, set())
    }

    return alerts, {
        "roles_codigo": len(codigo),
        "roles_base": len(base),
        "permisos_solo_en_el_codigo": sum(len(v) for v in solo_codigo.values()),
        "permisos_solo_en_la_base": sum(len(v) for v in solo_base.values()),
        # Aviso, no alerta: no apaga ninguna pantalla.
        "detalle_solo_en_la_base": [
            {"rol": r, "permisos": p} for r, p in solo_base.items()
        ],
    }


@bp.route("/permisos-drift", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def permisos_drift():
    """`config/roles.py` contra `seguridad.permiso`. Sólo lectura."""
    from config.roles import ROLES

    codigo = {rol: set(perms) for rol, perms in ROLES}
    filas = db.fetch_all(
        """
        SELECT r.nombre_rol, p.nombre_opcion
          FROM seguridad.rol r
          JOIN seguridad.permiso p ON p.id_rol = r.id_rol
        """
    ) or []
    base: dict[str, set] = {}
    for f in filas:
        base.setdefault(f["nombre_rol"], set()).add(f["nombre_opcion"])

    alerts, stats = _evaluar_drift_permisos(codigo, base)
    return jsonify({"ok": not alerts, "alerts": alerts, "stats": stats})



# ---------------------------------------------------------------------------
# Codigos de cliente repetidos
# ---------------------------------------------------------------------------

#: Los codigos de 3 letras que tienen mas de una ficha, con la plata que cuelga
#: de ellos. Es la MISMA definicion que usa /admin/clientes-asinfo: normalizada
#: con UPPER(TRIM(...)) porque asi JOINea todo el sistema.
SQL_CODIGOS_REPETIDOS = """
WITH dup AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n_fichas
      FROM scintela.cliente
     WHERE COALESCE(TRIM(codigo_cli), '') <> ''
     GROUP BY UPPER(TRIM(codigo_cli))
    HAVING COUNT(*) > 1
),
fact AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n
      FROM scintela.factura
     GROUP BY UPPER(TRIM(codigo_cli))
),
chq AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n
      FROM scintela.cheque
     GROUP BY UPPER(TRIM(codigo_cli))
)
SELECT dup.cod                 AS codigo_cli,
       dup.n_fichas            AS n_fichas,
       COALESCE(fact.n, 0)     AS n_facturas,
       COALESCE(chq.n, 0)      AS n_cheques
  FROM dup
  LEFT JOIN fact ON fact.cod = dup.cod
  LEFT JOIN chq  ON chq.cod  = dup.cod
 ORDER BY dup.cod
"""


def _texto_repetidos(filas: list[dict]) -> tuple[str, str]:
    """El titulo y el detalle del aviso, en castellano y sin siglas."""
    n_cod = len(filas)
    n_fichas = sum(int(f.get("n_fichas") or 0) for f in filas)
    n_mov = sum(int(f.get("n_facturas") or 0) + int(f.get("n_cheques") or 0)
                for f in filas)
    codigos = ", ".join(str(f.get("codigo_cli") or "") for f in filas)
    titulo = (f"{n_cod} codigo de cliente repetido" if n_cod == 1
              else f"{n_cod} codigos de cliente repetidos")
    partes = [f"{n_fichas} fichas", codigos]
    if n_mov:
        partes.insert(1, f"{n_mov} facturas y cheques mezclados")
    return titulo, " - ".join(p for p in partes if p)


def _avisar_repetidos(filas: list[dict]) -> None:
    """Deja el aviso en la campanita. Nunca rompe el health.

    La `clave` lleva los codigos, no la fecha: mientras la lista sea la misma
    el aviso NO se repite (es una deuda, no una novedad diaria), y en cuanto se
    resuelve uno vuelve a avisar diciendo cuantos quedan. Que es justamente la
    unica noticia que hay para dar.
    """
    if not filas:
        return
    try:
        from modules.avisos import avisar

        codigos = "|".join(sorted(str(f.get("codigo_cli") or "") for f in filas))
        titulo, detalle = _texto_repetidos(filas)
        avisar(fuente="clientes", nivel="alerta", titulo=titulo,
               detalle=detalle, cantidad=len(filas),
               # 🚨 La BARRA FINAL no es cosmética: el blueprint monta la
               # pantalla en "/admin/clientes-asinfo/", y sin ella el
               # `url_map` NO resuelve (tira RequestRedirect). La campanita
               # decide quién ve el aviso resolviendo este path contra el
               # url_map: sin resolver, se cae al permiso del TEMA
               # (`clientes.ver`) y se lo mostraría a gente que no puede abrir
               # la pantalla. En el navegador el link anda igual —redirige—
               # así que el error no se ve clickeando ni leyendo el código.
               url="/admin/clientes-asinfo/",
               clave=f"clientes:codigo-repetido:{codigos}")
    except Exception as exc:  # noqa: BLE001 -- avisar nunca rompe al que avisa
        _LOG_REPETIDOS.warning("no pude avisar los codigos repetidos: %s", exc)


@bp.route("/codigos-duplicados", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def codigos_duplicados():
    """Codigos de cliente repetidos: dos empresas bajo el mismo string.

    TMT 2026-08-24 (duena): *"resolver los 7 codigos de cliente duplicados por
    las pantallas. ponelo como alarma"*. Un codigo repetido **duplica la
    plata**, porque todo el sistema JOINea por `codigo_cli` y no por
    `id_cliente`: la comision de un vendedor se inflo $ 4.341,86 y el estado de
    cuenta de GUF mostro el mismo saldo dos veces. La migracion 0155 puso un
    indice unico que impide crear repetidos NUEVOS, pero dejo 7 exceptuados
    porque cada uno necesita una decision que no la toma el programa.

    Este bloque no arregla nada: se asegura de que la deuda **no se olvide**
    mientras siga abierta, y deja el aviso en la campanita con el link a la
    pantalla que la resuelve.
    """
    filas = [dict(r) for r in (db.fetch_all(SQL_CODIGOS_REPETIDOS) or [])]
    alerts: list = []
    if filas:
        titulo, detalle = _texto_repetidos(filas)
        alerts.append({
            "severity": "high", "category": "codigo_cliente_repetido",
            "msg": (f"{titulo}. {detalle}. Mientras esten repetidos, la plata "
                    f"de las dos empresas se suma bajo el mismo codigo. Se "
                    f"resuelven en /admin/clientes-asinfo/.")})
        _avisar_repetidos(filas)
    return jsonify({
        "ok": not filas,
        "alerts": alerts,
        "stats": {
            "n_codigos": len(filas),
            "n_fichas": sum(int(f.get("n_fichas") or 0) for f in filas),
            "codigos": [f.get("codigo_cli") for f in filas],
            "detalle": filas,
        },
    })


# ---------------------------------------------------------------------------
# La competencia de saldos cuenta lo que tiene que contar
# ---------------------------------------------------------------------------

SQL_COMPETENCIA = """
WITH pv AS (SELECT * FROM scintela.parado_venta WHERE cuenta),
     co AS (SELECT * FROM scintela.parado_cohorte WHERE NOT fuera)
SELECT
  (SELECT COALESCE(ROUND(SUM(kg)::numeric, 2), 0) FROM pv)                AS kg_venta,
  (SELECT COALESCE(ROUND(SUM(kg_vendidos)::numeric, 2), 0)
     FROM scintela.parado_foto)                                           AS kg_foto,
  (SELECT COUNT(*) FROM pv WHERE vendedor <> ALL(%s))                     AS n_ajenos,
  (SELECT COALESCE(ROUND(SUM(kg)::numeric, 2), 0)
     FROM pv WHERE vendedor <> ALL(%s))                                   AS kg_ajenos,
  (SELECT COUNT(*) FROM (
      SELECT pv.subcategoria, pv.color
        FROM pv JOIN co ON co.subcategoria = pv.subcategoria
                       AND co.color = pv.color
       GROUP BY 1, 2
      HAVING SUM(pv.kg) > MIN(co.kg_al_marcar) + 0.01) x)                 AS n_pasados,
  (SELECT COUNT(DISTINCT pv.subcategoria)
     FROM pv LEFT JOIN scintela.parado_punto pp
            ON pp.subcategoria = pv.subcategoria
    WHERE pp.subcategoria IS NULL)                                        AS n_sin_puntaje,
  (SELECT COUNT(*) FROM co WHERE motivo IS NULL)                          AS n_sin_motivo,
  (SELECT COUNT(*) FROM co WHERE kg_al_marcar IS NULL)                    AS n_sin_tope
"""


@bp.route("/competencia", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def competencia_coherente():
    """Que la competencia de saldos siga contando lo que tiene que contar.

    TMT 2026-08-25 (dueña): *"que la contabilización a futuro no se dañe"*. La
    competencia se corre hasta el 31/12 y su cuenta se REHACE entera en cada
    refresco desde Asinfo: nadie la mira renglón por renglón, así que un cambio
    de datos allá se convierte en puntos de más acá sin que suene nada. Los
    cinco chequeos son los cinco agujeros que ya se taparon una vez —cada uno
    costó que la dueña mirara la pantalla y dijera "esto está mal"—, puestos a
    vigilar que no vuelvan.

    · el encabezado y el ranking miden lo MISMO (los kilos de `parado_venta`
      contra los de la foto). El 25/08 decían 381 y 230: la diferencia eran
      152 kg firmados por alguien que no compite;
    · nadie fuera de los siete escribe kilos que puntúan;
    · ningún ítem sacó más kilos de los que tenía el día que entró (el tope);
    · ninguna tela vendida se quedó sin su puntaje congelado — sin él vale 1
      punto por kilo en silencio, que para una tela difícil es diez veces
      menos;
    · ningún ítem de la cohorte quedó sin `motivo` ni sin `kg_al_marcar`: sin
      motivo cuenta también la primera, y sin kilos al marcar no tiene tope.
    """
    from modules.analisis.queries import COMPETIDORES
    fila = db.fetch_one(SQL_COMPETENCIA, (COMPETIDORES, COMPETIDORES)) or {}
    return jsonify(competencia_alertas(fila))


def competencia_alertas(fila: dict) -> dict:
    """Las alarmas de una fila de números, sin base ni request.

    Va aparte de la ruta a propósito: la ruta está detrás de `@requiere_login`
    y `@requiere_permiso`, y un test que las tenga que atravesar termina
    levantando la app entera contra un Postgres. Lo que hay que probar son las
    cinco reglas, no el candado."""
    kg_venta = float(fila.get("kg_venta") or 0)
    kg_foto = float(fila.get("kg_foto") or 0)
    n_ajenos = int(fila.get("n_ajenos") or 0)
    n_pasados = int(fila.get("n_pasados") or 0)
    n_sin_puntaje = int(fila.get("n_sin_puntaje") or 0)
    n_sin_motivo = int(fila.get("n_sin_motivo") or 0)
    n_sin_tope = int(fila.get("n_sin_tope") or 0)
    alerts: list = []
    if abs(kg_venta - kg_foto) > 0.01:
        alerts.append({
            "severity": "high", "category": "competencia_descuadrada",
            "msg": (f"La competencia no cierra: los renglones suman "
                    f"{kg_venta:,.2f} kg y la foto {kg_foto:,.2f}. El "
                    f"encabezado y el ranking van a decir números distintos.")})
    if n_ajenos:
        alerts.append({
            "severity": "high", "category": "competencia_vendedor_ajeno",
            "msg": (f"{n_ajenos} ventas por {float(fila.get('kg_ajenos') or 0):,.2f} kg "
                    f"las firmó alguien que no está entre los siete: suman al "
                    f"total y no le suman a nadie en el ranking.")})
    if n_pasados:
        alerts.append({
            "severity": "high", "category": "competencia_sin_tope",
            "msg": (f"{n_pasados} ítems puntuaron más kilos de los que tenían "
                    f"el día que entraron a la lista: son kilos tejidos "
                    f"después, no saldo destrabado.")})
    if n_sin_puntaje:
        alerts.append({
            "severity": "medium", "category": "competencia_sin_puntaje",
            "msg": (f"{n_sin_puntaje} telas vendidas no tienen puntaje "
                    f"congelado: están valiendo 1 punto por kilo sin que nadie "
                    f"lo haya decidido.")})
    if n_sin_motivo or n_sin_tope:
        alerts.append({
            "severity": "medium", "category": "competencia_cohorte_incompleta",
            "msg": (f"{n_sin_motivo} ítems de la lista no tienen motivo y "
                    f"{n_sin_tope} no tienen kilos al marcar: los primeros "
                    f"cuentan también la primera, los segundos no tienen tope.")})
    return {
        "ok": not alerts,
        "alerts": alerts,
        "stats": {"kg_venta": kg_venta, "kg_foto": kg_foto,
                  "n_ajenos": n_ajenos, "n_pasados": n_pasados,
                  "n_sin_puntaje": n_sin_puntaje,
                  "n_sin_motivo": n_sin_motivo, "n_sin_tope": n_sin_tope},
    }


# ---------------------------------------------------------------------------
# Saldos: Al arrancar - Vendido = Queda sigue cerrando
# ---------------------------------------------------------------------------


@bp.route("/saldos", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def saldos_coherente():
    """Que la resta de `/analisis/parado` siga cerrando sola.

    TMT 2026-08-31 (dueña, tras corregir la resta): *"pone alertas después
    para que no nos vuelva a pasar"*. `resumen()` calcula "Al arrancar" con un
    PISO (ver su docstring) que absorbe, en silencio, tanto una tela nueva que
    se suma a la cohorte como la SEGUNDA que sigue entrando a una tela ya
    marcada — así la resta nunca se rompe a la vista. Pero ese mismo piso
    puede absorber también lo que ninguna venta explica de verdad: kilos que
    salieron de bodega sin quedar en `kg_vendidos` (un ajuste, un recuento,
    algo sin factura). `kg_movido` es justamente ese residuo — por
    construcción nunca da negativo, así que la pregunta no es "¿cerró?" sino
    "¿cuánto quedó sin explicar?". La pantalla ya lo muestra bajo Queda
    (`kg_movido >= 1`); este chequeo vigila lo mismo aunque nadie la mire ese
    día — recalcula EXACTAMENTE lo que ve `/analisis/parado` (mismos `items`,
    `con_puntos`, `kg_al_marcar_vivo` y `largada`).
    """
    from datetime import date
    from modules.analisis import queries as _saldos_q
    base = _saldos_q.con_puntos(_saldos_q.items())
    resumen = _saldos_q.resumen(
        base, _saldos_q.kg_al_marcar_vivo(base),
        largada=date.fromisoformat(_saldos_q.config("largada", "2026-08-25")))
    return jsonify(saldos_alertas(resumen))


def saldos_alertas(resumen: dict) -> dict:
    """La alarma de una tarjeta `resumen()` ya calculada, sin base ni request.

    Toma el dict que devuelve `queries.resumen()` — no una fila de SQL — así
    un test la prueba con datos sueltos, igual que las filas de prueba de
    `resumen()` mismo."""
    kg_movido = float(resumen.get("kg_movido") or 0)
    alerts: list = []
    if kg_movido >= 1:
        alerts.append({
            "severity": "medium", "category": "saldos_kg_sin_explicar",
            "msg": (f"Saldos tiene {kg_movido:,.2f} kg que ninguna venta "
                    f"explica: salieron de bodega sin quedar en "
                    f"`kg_vendidos` (un ajuste, un recuento, algo sin "
                    f"factura). La pantalla ya lo muestra bajo Queda; esto "
                    f"lo vigila aunque nadie la mire ese día.")})
    return {
        "ok": not alerts,
        "alerts": alerts,
        "stats": {"kg_movido": kg_movido},
    }


# ---------------------------------------------------------------------------
# Total Activo tiene que cerrar contra Pasivo + Patrimonio en la última fila
# de scintela.historia -- identidad contable básica, cierta en CUALQUIER
# snapshot (de cierre o diario), no sólo al cierre.
# ---------------------------------------------------------------------------
#
# Tamara 2026-09-02 ("no puede volver a pasar"): el incidente de agosto 2026
# (ancla-agosto-manual dejó anticipos/maquinaria/realty pisados con julio)
# desbalanceaba $976K entre Total Activo y Pasivo+Patrimonio, y nadie lo vio
# hasta que Tamara preguntó por qué Ventas estaba mal en Historia. Esta
# identidad HUBIERA prendido la alarma sola el mismo 01/09. No reemplaza el
# watchdog de utilidad (que compara live contra snapshot); esto compara la
# fila guardada CONTRA SÍ MISMA.
_HISTORIA_BALANCE_TOL_US = 1000.0


@bp.route("/historia-balance-cierra", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def historia_balance_cierra():
    """Alerta si, en la última fila de `scintela.historia`, Total Activo
    (banco+cart+anticipos+ustock+uqui+maquinaria+realty) no cierra contra
    Pasivo (deuda) + Patrimonio."""
    alerts = []
    stats = {}

    row = db.fetch_one(
        """
        SELECT id_historia, fecha, banco, cart, anticipos, ustock, uqui,
               maquinaria, realty, deuda, patrimonio, usuario_crea
          FROM scintela.historia
         ORDER BY fecha DESC, id_historia DESC
         LIMIT 1
        """
    )
    if not row:
        return jsonify({"ok": True, "alerts": alerts, "stats": stats})

    total_activo = (
        float(row.get("banco") or 0) + float(row.get("cart") or 0)
        + float(row.get("anticipos") or 0) + float(row.get("ustock") or 0)
        + float(row.get("uqui") or 0) + float(row.get("maquinaria") or 0)
        + float(row.get("realty") or 0)
    )
    esperado = float(row.get("deuda") or 0) + float(row.get("patrimonio") or 0)
    delta = total_activo - esperado

    stats["id_historia"] = row.get("id_historia")
    stats["fecha"] = str(row.get("fecha"))
    stats["usuario_crea"] = row.get("usuario_crea")
    stats["total_activo"] = round(total_activo, 2)
    stats["pasivo_mas_patrimonio"] = round(esperado, 2)
    stats["delta"] = round(delta, 2)

    if abs(delta) >= _HISTORIA_BALANCE_TOL_US:
        alerts.append({
            "severity": "high",
            "category": "balance_no_cierra",
            "msg": (
                f"scintela.historia id={row.get('id_historia')} "
                f"({row.get('fecha')}, usuario_crea={row.get('usuario_crea')}): "
                f"Total Activo − (Pasivo+Patrimonio) = {delta:+,.2f}. "
                f"Revisar banco/cart/anticipos/ustock/uqui/maquinaria/realty "
                f"contra un papel verificado -- mismo síntoma que el "
                f"incidente de agosto 2026."
            ),
        })

    return jsonify({"ok": len(alerts) == 0, "alerts": alerts, "stats": stats})


# ---------------------------------------------------------------------------
# Precios de /precios vs la lista vigente de Asinfo
# ---------------------------------------------------------------------------
# TMT 2026-09-02 (Tamara): si Asinfo cambia la lista y nadie trae los cambios,
# la hoja impresa y la proforma cotizan a otro precio del que se factura. La
# campanita avisa cuando sale una versión nueva; esto vigila que la diferencia
# no quede abierta. Si Asinfo no contesta, ok con `sin_datos` (no es un
# problema contable — ver [[feedback_flujo_chequeo_coherencia]]).


@bp.route("/precios-asinfo", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def precios_asinfo():
    from modules.precios import asinfo_lista
    return jsonify(asinfo_lista.health())


# ---------------------------------------------------------------------------
# Endpoint combinado: /admin/health/all (para un unico curl del cron)
# ---------------------------------------------------------------------------


@bp.route("/all", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def health_all():
    """JSON consolidado de las tres auditorías."""
    # Llamada interna sin redirect — usamos las funciones directamente.
    import json
    resp1 = usuario_crea_audit()
    resp2 = utilidad_watchdog()
    resp3 = cartera_coherence()
    resp4 = snapshot_diario_health()
    resp6 = cadena_saldos()
    resp7 = pendientes_conciliacion()
    resp9 = deposito_sin_fechaout()
    resp10 = hilado_ukg_reconstruible()
    resp11 = traza_fresca()
    resp12 = permisos_drift()
    resp14 = espejo_huerfano()
    resp15 = codigos_duplicados()
    resp16 = competencia_coherente()
    resp17 = devuelto_sin_nd()
    resp19 = saldos_coherente()
    resp20 = historia_balance_cierra()
    resp21 = precios_asinfo()
    data1 = json.loads(resp1.get_data(as_text=True))
    data2 = json.loads(resp2.get_data(as_text=True))
    data3 = json.loads(resp3.get_data(as_text=True))
    data4 = json.loads(resp4.get_data(as_text=True))
    data6 = json.loads(resp6.get_data(as_text=True))
    data7 = json.loads(resp7.get_data(as_text=True))
    data9 = json.loads(resp9.get_data(as_text=True))
    data10 = json.loads(resp10.get_data(as_text=True))
    data11 = json.loads(resp11.get_data(as_text=True))
    data12 = json.loads(resp12.get_data(as_text=True))
    data14 = json.loads(resp14.get_data(as_text=True))
    data15 = json.loads(resp15.get_data(as_text=True))
    data16 = json.loads(resp16.get_data(as_text=True))
    data17 = json.loads(resp17.get_data(as_text=True))
    data19 = json.loads(resp19.get_data(as_text=True))
    data20 = json.loads(resp20.get_data(as_text=True))
    data21 = json.loads(resp21.get_data(as_text=True))
    # TMT 2026-07-09 (dueña "no debería cargarse automático?"): el cron diario
    # aplica las retenciones de Asinfo de los últimos 60 días. Las retenciones
    # llegan DESPUÉS de la factura (cuando el cliente paga/retiene), así que un
    # pase diario idempotente es lo que las agarra sin que nadie apriete nada.
    # Mismo patrón que snapshot_diario (que también escribe en este cron).
    data5 = _aplicar_retenciones_asinfo_cron(dias=60)
    # TMT 2026-08-03: refresco del espejo de mails de clientes (los que Asinfo
    # usa para mandar la factura electrónica). Mismo criterio que las
    # retenciones: un pase diario idempotente. NO entra al `ok` general — que
    # Metabase esté caído un día no es un problema contable y no tiene que
    # encender el panel. Ver [[feedback_flujo_chequeo_coherencia]].
    data8 = _refrescar_mails_asinfo_cron()
    # TMT 2026-08-11 (dueña): las proformas se guardan UNA SEMANA. Va acá y no
    # en un cron nuevo — es el mismo pase diario idempotente que las
    # retenciones y los mails. NO entra al `ok`: borrar cero proformas es el
    # caso normal, no una alarma.
    data13 = _purgar_proformas_cron()
    # TMT 2026-08-30: espejo de la PRIMERA factura por cliente en Asinfo
    # ("cliente desde" de la ficha). Mismo pase diario idempotente que los
    # mails, y por lo mismo NO entra al `ok`: Metabase caído un día no es un
    # problema contable.
    data18 = _refrescar_primera_compra_asinfo_cron()
    return jsonify({
        "ok": (data1["ok"] and data2["ok"] and data3["ok"] and data4["ok"]
               and data6["ok"] and data7["ok"] and data9["ok"]
               and data10["ok"] and data11["ok"] and data12["ok"]
               and data14["ok"] and data15["ok"]
               and data16["ok"] and data17["ok"] and data19["ok"]
               and data20["ok"] and data21["ok"]),
        "usuario_crea_audit": data1,
        "utilidad_watchdog": data2,
        "cartera_coherence": data3,
        "snapshot_diario": data4,
        "retenciones_asinfo": data5,
        "cadena_saldos": data6,
        "pendientes_conciliacion": data7,
        "mails_asinfo": data8,
        "deposito_sin_fechaout": data9,
        "hilado_ukg": data10,
        "traza_fresca": data11,
        "permisos_drift": data12,
        "proformas_purga": data13,
        "espejo_huerfano": data14,
        "codigos_duplicados": data15,
        "competencia": data16,
        "devuelto_sin_nd": data17,
        "primera_compra_asinfo": data18,
        "saldos": data19,
        "historia_balance_cierra": data20,
        "precios_asinfo": data21,
    })


def _purgar_proformas_cron(dias: int = 7) -> dict:
    """Borra las proformas de más de `dias` días y dice cuántas quedan vivas.

    Las proformas son un papel de trabajo, no un documento contable: no tocan
    stock, facturas ni utilidad. La dueña las quiso con fecha de vencimiento
    (2026-08-11) para que la lista de recientes sea corta y útil. Fail-soft:
    si esto falla, el health sigue.
    """
    try:
        from modules.proformas import queries as _prof
        borradas = _prof.purgar_viejas(dias)
        vivas = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.proforma_cabecera"
        ) or {}
        return {"ok": True, "dias": dias, "borradas": borradas,
                "vivas": int(vivas.get("n") or 0)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _refrescar_mails_asinfo_cron() -> dict:
    """Copia el catálogo de mails de Asinfo a la tabla espejo. Fail-soft."""
    from modules.clientes import mail_asinfo
    return mail_asinfo.refrescar_cron()


def _refrescar_primera_compra_asinfo_cron() -> dict:
    """Copia la primera factura por cliente de Asinfo al espejo. Fail-soft."""
    from modules.clientes import primera_compra_asinfo
    return primera_compra_asinfo.refrescar_cron()


def _aplicar_retenciones_asinfo_cron(dias: int = 60) -> dict:
    """Aplica (idempotente) las retenciones de Asinfo de los últimos `dias`,
    y de paso barre UN mes viejo por día.

    🚨 EL AGUJERO QUE ESTO TAPA (TMT 2026-08-07). La consulta a Asinfo filtra
    por la fecha de la **FACTURA**, no por la del cobro — medido: pidiendo el
    1–10/05 vuelven sólo facturas del 4 al 8/05, ninguna de afuera. Como el
    cron pide siempre los últimos 60 días, una retención que Asinfo registre
    hoy contra una factura de hace tres meses **no la mira nadie, nunca**: ni
    el cron ni la pantalla (que además tenía piso 2026-06-01). Así quedaron
    huérfanas las de febrero a mayo, de cuando PC tomó la posta del dBase y
    sólo miró 60 días para atrás.

    El barrido recorre un mes viejo por día, rotando por el día del mes, así
    que en un mes cubre el año. Va de a un mes por dos razones medidas: pedirle
    a Asinfo un rango grande tarda 10-30 s (y un mes entero llegó a colgar la
    app con 502), y el cron no puede quedarse esperando.

    ⚠️ El barrido va con `solo_sin_abono=True`: en las facturas viejas la
    retención puede estar ya sumada adentro del abono (RETENCIO.PRG del dBase
    la metía ahí sin dejar fila en `scintela.retencion`, así que el guard `ya`
    no la ve) y aplicarla otra vez descontaría el saldo dos veces. Las que
    tienen abono se saltean y se miran de a una en
    `/facturas/retenciones-en-abono`.

    Para el cron diario (/admin/health/all). Fail-soft: cualquier excepción se
    devuelve como {ok:False, error:...} sin romper el health check.
    """
    try:
        from datetime import timedelta

        from filters import today_ec
        from modules.retenciones import queries as ret_q
        hoy = today_ec()
        r = ret_q.aplicar_retenciones_asinfo(
            hoy - timedelta(days=dias), hoy, usuario="cron-retenciones")
        r["ok"] = True
        r["barrido"] = _barrer_un_mes_viejo(hoy, dias)
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


#: Cuántos meses para atrás recorre el barrido, uno por día.
BARRIDO_MESES = 12


def _mes_del_barrido(hoy, dias_ventana: int = 60):
    """Qué mes viejo le toca hoy: `(primero, ultimo)` del mes elegido.

    Rota por el día del mes (1→el más viejo, 2→el siguiente…), así que no
    depende de que el cron haya corrido ayer ni de guardar estado en ningún
    lado: mirando la fecha se sabe qué le tocó. Nunca devuelve un mes que ya
    cubra la ventana de los últimos `dias_ventana`, para no pedir dos veces lo
    mismo en la misma corrida.
    """
    import calendar as _cal
    from datetime import date as _date
    from datetime import timedelta as _td

    piso = hoy - _td(days=dias_ventana)
    # meses_atras va de BARRIDO_MESES (el más viejo) hacia 1
    meses_atras = BARRIDO_MESES - ((hoy.day - 1) % BARRIDO_MESES)
    y, m = hoy.year, hoy.month - meses_atras
    while m <= 0:
        m += 12
        y -= 1
    primero = _date(y, m, 1)
    ultimo = _date(y, m, _cal.monthrange(y, m)[1])
    if ultimo >= piso:
        return None
    return primero, ultimo


def _barrer_un_mes_viejo(hoy, dias_ventana: int = 60) -> dict:
    """Aplica las retenciones huérfanas de UN mes viejo. Fail-soft."""
    rango = _mes_del_barrido(hoy, dias_ventana)
    if not rango:
        return {"ok": True, "salteado": "el mes del turno cae dentro de la ventana"}
    desde, hasta = rango
    try:
        from modules.retenciones import queries as ret_q
        r = ret_q.aplicar_retenciones_asinfo(
            desde, hasta, usuario="cron-retenciones-viejas", solo_sin_abono=True)
        r["ok"] = True
        r["mes"] = desde.strftime("%Y-%m")
        # `sin_factura` de un mes viejo es ruido conocido (facturas que PC nunca
        # cargó): no se arrastra al health, que ya tiene su propio bloque.
        r.pop("sin_factura", None)
        return r
    except Exception as e:
        return {"ok": False, "mes": desde.strftime("%Y-%m"), "error": str(e)[:200]}


@bp.route("/hilado-stock-debug", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def hilado_stock_debug():
    """READ-ONLY: vuelca el cálculo real del stock de HILADO del balance.

    Muestra, por cada compra tipo='H' del mes: el kg GUARDADO en la compra
    (no el de referencia de Asinfo), el importe y el $/kg implícito; cómo se
    arma el kcom (suma de compra.kg + kg reconstruido de la importación),
    el ucom, el $/kg ponderado de las compras del mes, y las compras que NO
    matchean una importación (sin_match). Sirve para ver de dónde sale el
    um_act que revalúa TODO el stock. TMT 2026-07-23 (dueña: encontrar el bug
    del $/kg sin adivinar).
    """
    from filters import today_ec

    hoy = today_ec()
    yy, mm = hoy.year, hoy.month
    _NOBF = "COALESCE(usuario_crea, '') <> 'asinfo-backfill'"

    rows = db.fetch_all(
        f"""
        SELECT id_compra, codigo_prov AS prov,
               NULLIF(regexp_replace(COALESCE(concepto,''),'[^0-9]','','g'),'')::bigint AS ref,
               fecha,
               COALESCE(kg, 0)      AS kg,
               COALESCE(importe, 0) AS importe,
               COALESCE(stat,'')    AS stat,
               COALESCE(usuario_crea,'') AS usuario_crea
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'H'
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND {_NOBF}
         ORDER BY fecha, id_compra
        """,
        (yy, mm),
    ) or []

    kcom_base = sum(float(r.get("kg") or 0) for r in rows)
    ucom = sum(float(r.get("importe") or 0) for r in rows)

    recon = {"kg": 0.0, "sin_match": [], "disponible": None, "error": None}
    try:
        from modules.importaciones import service as _svc
        _r = _svc.kg_hilado_faltantes_mes(rows)
        recon["kg"] = float(_r.get("kg") or 0)
        recon["sin_match"] = _r.get("sin_match") or []
        recon["disponible"] = _r.get("disponible")
    except Exception as e:  # noqa: BLE001
        recon["error"] = str(e)[:200]

    kg_add = float(recon.get("kg") or 0)
    kcom = kcom_base + kg_add

    # TMT 2026-07-31 — el camino NUEVO: `kg_hilado_mes` REEMPLAZA el SUM crudo
    # (kg 1 vez por GRUPO de partidas, ignorando `compra.kg` en las compras de
    # importación). Se muestra al lado del viejo para poder comparar los dos
    # números en la misma pantalla el día del cambio.
    nuevo = {"error": None}
    try:
        from modules.importaciones import service as _svc_n
        _n = _svc_n.kg_hilado_mes(rows)
        nuevo = {
            "kg": _n.get("kg"),
            "disponible": _n.get("disponible"),
            "grupos": _n.get("grupos"),
            "kg_de_importacion": _n.get("kg_de_importacion"),
            "kg_compra_ignorado": _n.get("kg_compra_ignorado"),
            "n_sin_match": len(_n.get("sin_match") or []),
            "avisos": _n.get("avisos") or [],
            "fuera_de_banda": _n.get("fuera_de_banda") or [],
            "delta_vs_sum_crudo": round(float(_n.get("kg") or 0) - kcom_base, 2),
            "error": None,
        }
    except Exception as e:  # noqa: BLE001
        nuevo = {"error": str(e)[:200]}

    close = db.fetch_one(
        "SELECT fecha, stock, ustock FROM scintela.historia ORDER BY fecha DESC LIMIT 1"
    ) or {}
    _stk = float(close.get("stock") or 0)
    um0 = (float(close.get("ustock") or 0) / _stk) if _stk else 0.0

    live = {}
    try:
        from modules.informes import queries as _iq
        _bal = _iq.informe_balance()
        _comp = (_bal.get("diagnostico", {}) or {}).get("componentes", {}) or {}
        live = {
            "vsto": _comp.get("vsto"),
            "utilidad": _comp.get("utilidad"),
            "patr": _comp.get("patr"),
        }
    except Exception as e:  # noqa: BLE001
        live = {"error": str(e)[:200]}

    def _uxk(imp, kg):
        kg = float(kg or 0)
        return round(float(imp or 0) / kg, 3) if kg else None

    compras = [
        {
            "id": r.get("id_compra"),
            "fecha": str(r.get("fecha")),
            "prov": r.get("prov"),
            "ref": r.get("ref"),
            "kg_guardado": round(float(r.get("kg") or 0), 2),
            "importe": round(float(r.get("importe") or 0), 2),
            "usd_kg": _uxk(r.get("importe"), r.get("kg")),
            "stat": r.get("stat"),
            "usuario": r.get("usuario_crea"),
        }
        for r in rows
    ]

    # ── ESCENARIOS: cómo valuaría el balance el hilado con cada versión de kcom ──
    from modules.informes import queries as _iq2
    HI0 = 0.0
    um0_ini = 0.0
    try:
        HI0 = float(_iq2.tarifa_iniciales_mes_anterior(mm, yy, "hilado") or 0)
        um0_ini = float(_iq2.tarifa_iniciales_mes_anterior(mm, yy, "um") or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        comp_bal = _iq2.compras_mes_corriente()
    except Exception as e:  # noqa: BLE001
        comp_bal = {"error": str(e)[:120]}
    kcom_bal = float(comp_bal.get("kg") or 0)
    ucom_bal = float(comp_bal.get("importe") or 0)
    kcom_ded = 0.0
    try:
        from modules.importaciones import service as _svc2
        _porprov = _svc2.kg_stock_por_compra(rows)
        kcom_ded = sum(float(v or 0) for v in (_porprov or {}).values())
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.importaciones import service as _svc3
        rec = _svc3.costo_hilado_recibido_mes(yy, mm)
    except Exception as e:  # noqa: BLE001
        rec = {"error": str(e)[:120]}

    def _um_act(kc, uc):
        den = HI0 + kc
        return round((HI0 * um0_ini + uc) / den, 4) if den else None

    # ── UTILIDAD PROYECTADA por escenario — corre el balance REAL con cada
    # (kg, importe) de compras via comp_mes_override (read-only, no muta nada).
    def _util_scn(kc, uc):
        try:
            _b = _iq2.informe_balance(comp_mes_override={"kg": float(kc or 0), "importe": float(uc or 0)})
            _c = (_b.get("diagnostico", {}) or {}).get("componentes", {}) or {}
            return {
                "utilidad": round(float(_c.get("utilidad") or 0), 2),
                "vsto": round(float(_c.get("vsto") or 0), 2),
                "patr": round(float(_c.get("patr") or 0), 2),
            }
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:160]}

    util_A = _util_scn(kcom_bal, ucom_bal)
    util_B = _util_scn(kcom_ded, ucom_bal)
    util_C = _util_scn(float(rec.get("kg") or 0), float(rec.get("us") or 0)) if rec.get("kg") else {"error": "sin rec"}

    # ── HISTORIA día-a-día: ¿la caída de stock fue por KG o por $/kg? ──
    hist_dia = db.fetch_all(
        """
        SELECT fecha, stock, ustock, uqui, usuti, patrimonio
          FROM scintela.historia
         ORDER BY fecha DESC
         LIMIT 8
        """
    ) or []
    hist_trend = []
    for h in hist_dia:
        _s = float(h.get("stock") or 0)
        _u = float(h.get("ustock") or 0)
        hist_trend.append({
            "fecha": str(h.get("fecha")),
            "stock_kg": round(_s, 0),
            "ustock": round(_u, 0),
            "ustock_por_kg": round(_u / _s, 4) if _s else None,
            "uqui": round(float(h.get("uqui") or 0), 0),
            "usuti": round(float(h.get("usuti") or 0), 0),
            "patrimonio": round(float(h.get("patrimonio") or 0), 0),
        })

    escenarios = {
        "HI0_stock_inicial_kg": round(HI0, 2),
        "um0_stock_inicial_usdkg": round(um0_ini, 4),
        "A_balance_actual": {
            "fuente": "compras_mes_corriente (SUM crudo compra.kg)",
            "kcom": round(kcom_bal, 2), "ucom": round(ucom_bal, 2),
            "usd_kg_compras": round(ucom_bal / kcom_bal, 4) if kcom_bal else None,
            "um_act": _um_act(kcom_bal, ucom_bal),
            "utilidad_proyectada": util_A,
        },
        "B_dedup_kg_por_importacion": {
            "fuente": "kg_stock_por_compra (kg 1 vez/importacion) + TODOS los importes",
            "kcom": round(kcom_ded, 2), "ucom": round(ucom_bal, 2),
            "usd_kg_compras": round(ucom_bal / kcom_ded, 4) if kcom_ded else None,
            "um_act": _um_act(kcom_ded, ucom_bal),
            "utilidad_proyectada": util_B,
        },
        "C_recibido_mes": {
            "fuente": "costo_hilado_recibido_mes (kg fisico recibido + su costo)",
            "kcom": rec.get("kg"), "ucom": rec.get("us"),
            "usd_kg_compras": rec.get("usd_kg"),
            "um_act": _um_act(float(rec.get("kg") or 0), float(rec.get("us") or 0)) if rec.get("kg") else None,
            "utilidad_proyectada": util_C,
        },
        "nota": "um_act mueve el $/kg de TODO el stock; delta_valor ~= delta_um_act * kg_total_stock",
    }

    return jsonify({
        "escenarios_valuacion": escenarios,
        "mes": f"{yy}-{mm:02d}",
        "n_compras_hilado": len(rows),
        "kcom_base_sum_compra_kg": round(kcom_base, 2),
        "kg_reconstruido_de_importacion": round(kg_add, 2),
        "kcom_total_usado_en_balance": round(kcom, 2),
        "kg_hilado_mes_NUEVO": nuevo,
        "ucom_total_importe": round(ucom, 2),
        "usd_kg_ponderado_compras_mes": round(ucom / kcom, 4) if kcom else None,
        "sin_match_n": len(recon.get("sin_match") or []),
        "sin_match": [
            {"id": s.get("id_compra"), "prov": s.get("prov"), "ref": s.get("ref"),
             "importe": round(float(s.get("importe") or 0), 2)}
            for s in (recon.get("sin_match") or [])
        ],
        "asinfo_disponible": recon.get("disponible"),
        "recon_error": recon.get("error"),
        "ultimo_cierre_um0_ref": {"fecha": str(close.get("fecha")), "um0": round(um0, 4)},
        "balance_live": live,
        "historia_dia_a_dia": hist_trend,
        "compras": compras,
    })
