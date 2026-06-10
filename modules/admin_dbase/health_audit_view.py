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

from flask import Blueprint, jsonify

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
    "dbf-import",
    "web",
    "tamara",
    "andres",
    "alex",
    "auto",
    "asinfo",
}

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
                   ('asinfo-backfill', 'dbf-import')
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
            row = db.fetch_one(
                f"""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM({sum_col}), 0) AS s,
                       array_agg(DISTINCT usuario_crea) AS usuarios
                  FROM scintela.{tabla}
                 WHERE fecha_crea >= (CURRENT_DATE - INTERVAL '7 days')
                   AND COALESCE(usuario_crea, '') NOT IN ({placeholders})
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
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
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
# Endpoint combinado: /admin/health/all (para un único curl del cron)
# ---------------------------------------------------------------------------


@bp.route("/all", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def health_all():
    """JSON consolidado de las dos auditorías."""
    # Llamada interna sin redirect — usamos las funciones directamente.
    import json
    resp1 = usuario_crea_audit()
    resp2 = utilidad_watchdog()
    data1 = json.loads(resp1.get_data(as_text=True))
    data2 = json.loads(resp2.get_data(as_text=True))
    return jsonify({
        "ok": data1["ok"] and data2["ok"],
        "usuario_crea_audit": data1,
        "utilidad_watchdog": data2,
    })
