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

from flask import Blueprint, jsonify, request

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("health_audit", __name__, url_prefix="/admin/health")


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
@bp.route("/snapshot-diario", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def snapshot_diario_health():
    """Toma la FOTO DIARIA del balance vivo y la valida contra el día anterior.

    Esto es lo que independiza a PC del dBase: capturar cada día en vivo (cuando
    cartera/anticipos/stock están frescos) para que el cierre de mes sea, sin
    reconstruir nada, la foto del último día. Corre por el cron del health.

    Alerta si:
      - el patrimonio pega un salto > $500k día a día (posible bug de cálculo)
      - el stock (USTOCK) salta > 5% día a día
      - el stock de TERMINADO/USTOCK sale en 0 (bug de iniciales/mes)
      - el patrimonio sale <= 0
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
        return jsonify({"ok": False, "alerts": alerts + [f"snapshot diario falló: {e}"], "stats": stats})

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

    return jsonify({"ok": len(alerts) == 0, "alerts": alerts, "stats": stats})


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
    if guardada and _row:
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
            "nota": ("`patrimonio` va NETO de retiros (patr − usret), igual que "
                     "el dBase (REPLA PATRIMONIO WITH PATR-URET) y que la foto "
                     "diaria. El Δ es lo que se corrige al rehacer la foto — "
                     "acá no se escribió nada."),
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


def _breaks_cadena(no_banco: int, desde) -> list[dict]:
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
    """
    import bank_helpers
    return bank_helpers.contar_quiebres(no_banco=no_banco, desde_fecha=desde)


def _evaluar_cadena(*, no_banco, nombre, stored, signed, breaks, n_nulls, dias,
                    derivado=None, origen=None, columna_running=None):
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
            breaks = _breaks_cadena(no_banco, desde)
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


# Endpoint combinado: /admin/health/all (para un único curl del cron)
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
    data1 = json.loads(resp1.get_data(as_text=True))
    data2 = json.loads(resp2.get_data(as_text=True))
    data3 = json.loads(resp3.get_data(as_text=True))
    data4 = json.loads(resp4.get_data(as_text=True))
    data6 = json.loads(resp6.get_data(as_text=True))
    data7 = json.loads(resp7.get_data(as_text=True))
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
    return jsonify({
        "ok": (data1["ok"] and data2["ok"] and data3["ok"] and data4["ok"]
               and data6["ok"] and data7["ok"]),
        "usuario_crea_audit": data1,
        "utilidad_watchdog": data2,
        "cartera_coherence": data3,
        "snapshot_diario": data4,
        "retenciones_asinfo": data5,
        "cadena_saldos": data6,
        "pendientes_conciliacion": data7,
        "mails_asinfo": data8,
    })


def _refrescar_mails_asinfo_cron() -> dict:
    """Copia el catálogo de mails de Asinfo a la tabla espejo. Fail-soft."""
    from modules.clientes import mail_asinfo
    return mail_asinfo.refrescar_cron()


def _aplicar_retenciones_asinfo_cron(dias: int = 60) -> dict:
    """Aplica (idempotente) las retenciones de Asinfo de los últimos `dias`.

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
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


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
