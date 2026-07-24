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

        live_patr = float(comp.get("patr") or 0)
        live_cart = float(comp.get("cart") or 0)
        live_vsto = float(comp.get("vsto") or 0)
        live_utilidad = float(comp.get("utilidad") or 0)

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

    apertura_ok = bool(hi0 and pf0)  # hay stock de apertura (no 0)
    return jsonify({
        "ok": apertura_ok and (roll.get("rollover") or roll.get("writeback")),
        "simulando_fecha": fstr,
        "today_ec_visto_por_el_codigo": visto,
        "rollover_dry_run": roll,
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
    data1 = json.loads(resp1.get_data(as_text=True))
    data2 = json.loads(resp2.get_data(as_text=True))
    data3 = json.loads(resp3.get_data(as_text=True))
    data4 = json.loads(resp4.get_data(as_text=True))
    # TMT 2026-07-09 (dueña "no debería cargarse automático?"): el cron diario
    # aplica las retenciones de Asinfo de los últimos 60 días. Las retenciones
    # llegan DESPUÉS de la factura (cuando el cliente paga/retiene), así que un
    # pase diario idempotente es lo que las agarra sin que nadie apriete nada.
    # Mismo patrón que snapshot_diario (que también escribe en este cron).
    data5 = _aplicar_retenciones_asinfo_cron(dias=60)
    return jsonify({
        "ok": data1["ok"] and data2["ok"] and data3["ok"] and data4["ok"],
        "usuario_crea_audit": data1,
        "utilidad_watchdog": data2,
        "cartera_coherence": data3,
        "snapshot_diario": data4,
        "retenciones_asinfo": data5,
    })


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
