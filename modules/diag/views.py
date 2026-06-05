"""Diagnóstico de los bridges externos (formulas_app + Asinfo).

Una sola pantalla `/diag/integraciones` que muestra preview de toda la data
que están trayendo las funciones de `modules/tintura/` y `modules/asinfo/`.

Propósito: vos abrís esa URL y validás de un vistazo que:
    - el bridge a formulas_app está vivo y trae órdenes + stock de químicos
    - el bridge a Metabase está vivo y trae facturas/devoluciones/NCs

Permiso: `informes.ver` (rol Gerente / Dueño / Administrador / Contabilidad).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from filters import today_ec
from modules._lib import formulas_db, metabase_client
from modules.asinfo import service as asinfo
from modules.tintura import service as tintura

_LOG = logging.getLogger("programa_core.diag")

bp = Blueprint("diag", __name__, url_prefix="/diag", template_folder="templates")


def _parse_fecha(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s.strip()[:10])
    except (ValueError, AttributeError):
        return default


@bp.route("/integraciones", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def integraciones():
    """Pantalla unificada de diagnóstico de los bridges."""
    # Rango por default: hoy
    hoy = today_ec()
    fecha_desde = _parse_fecha(request.args.get("desde"), hoy)
    fecha_hasta = _parse_fecha(request.args.get("hasta"), hoy)

    # Estado de los bridges (sin tocar la red más allá de lo que ya se hace)
    estado = {
        "formulas_db_configured": formulas_db.disponible(),
        "metabase_configured": metabase_client.disponible(),
    }

    # ---- Bridge formulas_app ----
    # Últimas 25 órdenes (cualquier estado)
    tintura_ordenes = tintura.tinturado_resumen(limite=25)
    # Stock químicos (todos, ordenados por familia/num_visible)
    tintura_stock_baseline = tintura.stock_quimicos()

    # ---- Bridge Asinfo (cards Metabase) ----
    # Totales por tipo en el rango pedido
    asinfo_totales = asinfo.facturas_totales_por_tipo(fecha_desde, fecha_hasta)

    # Primeras 50 facturas individuales del rango (para que veas la data cruda)
    asinfo_filas = asinfo.facturas_periodo(fecha_desde, fecha_hasta)[:50]

    return render_template(
        "diag/integraciones.html",
        estado=estado,
        tintura_ordenes=tintura_ordenes,
        tintura_stock=tintura_stock_baseline[:80],  # top 80 por familia
        tintura_stock_total=len(tintura_stock_baseline),
        asinfo_totales=asinfo_totales,
        asinfo_filas=asinfo_filas,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        hoy=hoy,
        ayer=hoy - timedelta(days=1),
        mes_actual_desde=hoy.replace(day=1),
    )
