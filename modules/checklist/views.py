"""Checklist del día — qué le falta cargar a PC vs la operación de ayer.

Pedido dueña 2026-06-12: "quiero que la experiencia sea la mejor posible,
son reluctantes a cambiar del programa viejo". Mientras conviven los dos
programas, el doble-tipeo es el costo real del cambio. Este panel lo
convierte en una lista corta: cada mañana muestra qué quedó sin replicar
en PC (facturas Asinfo sin cargar, parte K de tejido, planilla de tintura,
cheques con fecha de depósito vencida) con link directo a la pantalla
de carga. Todo se calcula con data local + bridge Asinfo — NO necesita
tarball ni sync.

SOLO LECTURA. Si el bridge Asinfo no responde, esa fila degrada a
"no disponible" y el resto del checklist sigue (fail-soft, no fail-closed:
esto es un recordatorio, no un número contable).
"""
from __future__ import annotations

import re
from datetime import timedelta

from flask import Blueprint, render_template

import db
from auth import requiere_login, requiere_permiso
from filters import today_ec

checklist_bp = Blueprint(
    "checklist", __name__, template_folder="templates",
)


def _dia_operativo_anterior(hoy):
    """Ayer; si ayer fue domingo, el sábado (la fábrica trabaja sábados)."""
    ayer = hoy - timedelta(days=1)
    if ayer.weekday() == 6:  # domingo
        ayer -= timedelta(days=1)
    return ayer


_RE_DIGITS = re.compile(r"(\d+)\s*$")


def _numf_tail(s) -> int | None:
    """'001-002-000177294' → 177294. Mismo criterio que dbase-compare."""
    m = _RE_DIGITS.search(str(s or "").strip())
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n or None


def _facturas_asinfo_sin_cargar(ayer, hoy) -> dict:
    """Facturas en Asinfo (ayer+hoy) cuyo N° SRI no está en PC. Aproximado
    por numf — el detalle fino (aliases, NC) vive en /facturas/desde-asinfo,
    acá solo contamos para el recordatorio."""
    from modules.asinfo import service as asinfo_service

    rows = asinfo_service.facturas_periodo(ayer, hoy) or []
    asinfo = {}
    for r in rows:
        n = _numf_tail(r.get("numero"))
        if n:
            asinfo[n] = float(r.get("usd") or 0)
    if not asinfo:
        return {"count": 0, "usd": 0.0, "total_asinfo": 0}
    pc_rows = db.fetch_all(
        """
        SELECT numf_completo, numf FROM scintela.factura
         WHERE fecha >= %s
        """,
        (ayer - timedelta(days=7),),
    ) or []
    en_pc = set()
    for r in pc_rows:
        for k in ("numf_completo", "numf"):
            n = _numf_tail(r.get(k))
            if n:
                en_pc.add(n)
    faltan = {n: usd for n, usd in asinfo.items() if n not in en_pc}
    return {"count": len(faltan), "usd": sum(faltan.values()),
            "total_asinfo": len(asinfo)}


@checklist_bp.route("/checklist-dia")
@requiere_login
@requiere_permiso("cheques.ver")
def dia():
    hoy = today_ec()
    ayer = _dia_operativo_anterior(hoy)
    items = []

    # 1. Facturas Asinfo sin cargar (ayer + hoy).
    try:
        fa = _facturas_asinfo_sin_cargar(ayer, hoy)
        items.append({
            "titulo": "Facturas Asinfo sin cargar",
            "detalle": (f"{fa['count']} de {fa['total_asinfo']} facturas de Asinfo "
                        f"({ayer:%d/%m} y hoy) faltan en PC — ~$ {fa['usd']:,.2f}" if fa["count"]
                        else f"Las {fa['total_asinfo']} facturas de Asinfo de ayer y hoy ya están en PC"),
            "estado": "falta" if fa["count"] else "ok",
            "endpoint": "facturas.desde_asinfo",
            "accion": "Cargar desde Asinfo",
        })
    except Exception:  # noqa: BLE001 — bridge caído ≠ checklist caído
        items.append({
            "titulo": "Facturas Asinfo sin cargar",
            "detalle": "Asinfo no respondió — revisar a mano",
            "estado": "info",
            "endpoint": "facturas.desde_asinfo",
            "accion": "Abrir",
        })

    # 2. Parte K (kg de tejido) del día operativo anterior.
    k = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg
          FROM scintela.compra
         WHERE tipo = 'K' AND fecha = %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (ayer,),
    ) or {}
    k_ult = db.fetch_one(
        """
        SELECT fecha, COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg
          FROM scintela.compra
         WHERE tipo = 'K' AND COALESCE(stat, '') NOT IN ('X', 'Y')
         GROUP BY fecha ORDER BY fecha DESC LIMIT 1
        """
    ) or {}
    guia_k = (f" · último cargado: {k_ult['fecha']:%d/%m} "
              f"({float(k_ult.get('kg') or 0):,.0f} kg)" if k_ult.get("fecha") else "")
    items.append({
        "titulo": f"Parte de tejido K del {ayer:%d/%m}",
        "detalle": (f"{float(k.get('kg') or 0):,.2f} kg cargados ({k.get('n')} partes)" + guia_k
                    if (k.get("n") or 0) else
                    "Sin parte K cargado — suele ser 1 parte de ~9.000-10.000 kg por día" + guia_k),
        "estado": "ok" if (k.get("n") or 0) else "falta",
        "endpoint": "compras.nueva",
        "accion": "Cargar compra K",
    })

    # 3. Planilla de tintura del día operativo anterior.
    t = db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS usd "
        "FROM scintela.tinto WHERE fecha = %s",
        (ayer,),
    ) or {}
    t_ult = db.fetch_one(
        """
        SELECT fecha, COUNT(*) AS n, COALESCE(SUM(importe), 0) AS usd
          FROM scintela.tinto
         GROUP BY fecha ORDER BY fecha DESC LIMIT 1
        """
    ) or {}
    guia_t = (f" · última cargada: {t_ult['fecha']:%d/%m} "
              f"({t_ult.get('n')} líneas, $ {float(t_ult.get('usd') or 0):,.0f})"
              if t_ult.get("fecha") else "")
    items.append({
        "titulo": f"Planilla de tintura del {ayer:%d/%m}",
        "detalle": (f"{t.get('n')} líneas / $ {float(t.get('usd') or 0):,.2f}" + guia_t
                    if (t.get("n") or 0) else "Sin planilla cargada para ese día" + guia_t),
        "estado": "ok" if (t.get("n") or 0) else "falta",
        "endpoint": "comparativa_tintoreria.tinto_carga",
        "accion": "Cargar planilla",
    })

    # 4. Cheques con fecha "a depositar" ya vencida y todavía en cartera.
    ch = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total,
               MIN(fechad) AS vieja
          FROM scintela.cheque
         WHERE stat = 'Z' AND fechad IS NOT NULL AND fechad <= %s
        """,
        (hoy,),
    ) or {}
    items.append({
        "titulo": "Depósitos pendientes de registrar",
        "detalle": (f"{ch.get('n')} cheques en cartera con fecha a depositar vencida "
                    f"($ {float(ch.get('total') or 0):,.2f}, el más viejo del "
                    f"{ch['vieja']:%d/%m}) — si ya fueron al banco, registrá el depósito"
                    if (ch.get("n") or 0) else "Ningún cheque con depósito vencido"),
        "estado": "falta" if (ch.get("n") or 0) else "ok",
        "endpoint": "cheques.lista",
        "accion": "Depositar lote",
    })

    # 5/6. Caja y banco de ayer — informativo (¿se replicó el día?).
    cj = db.fetch_one(
        "SELECT COUNT(*) FILTER (WHERE fecha = %s) AS ayer_n, "
        "COUNT(*) FILTER (WHERE fecha = %s) AS hoy_n FROM scintela.caja "
        "WHERE fecha IN (%s, %s)",
        (ayer, hoy, ayer, hoy),
    ) or {}
    bk = db.fetch_one(
        "SELECT COUNT(*) FILTER (WHERE fecha = %s) AS ayer_n, "
        "COUNT(*) FILTER (WHERE fecha = %s) AS hoy_n "
        "FROM scintela.transacciones_bancarias WHERE fecha IN (%s, %s)",
        (ayer, hoy, ayer, hoy),
    ) or {}
    items.append({
        "titulo": "Movimientos ya en PC (guía)",
        "detalle": (f"Caja — {ayer:%d/%m}: {cj.get('ayer_n') or 0} · hoy: {cj.get('hoy_n') or 0}   |   "
                    f"Banco — {ayer:%d/%m}: {bk.get('ayer_n') or 0} · hoy: {bk.get('hoy_n') or 0}"),
        "estado": "info",
        "endpoint": "caja.lista", "accion": "Ver caja",
    })

    pendientes = sum(1 for i in items if i["estado"] == "falta")
    return render_template("checklist/dia.html", items=items, hoy=hoy,
                           ayer=ayer, pendientes=pendientes)
