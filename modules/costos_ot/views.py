"""Rutas del módulo costos_ot.

Dos rutas ligeras:
    GET /costos-ot/cliente/<codigo_cli>  — listado filtrado por cliente
    GET /costos-ot/cliente/<codigo_cli>/fragment — partial HTMX para embed en
                                                   detalle de factura / cartera

La mayoría del uso es vía el partial en el detalle de factura; la vista full
queda por si el gerente quiere auditar todas las OTs cerradas de un cliente.
"""
from __future__ import annotations

from flask import Blueprint, render_template

from auth import requiere_login, requiere_permiso
from modules.costos_ot import service

costos_ot_bp = Blueprint(
    "costos_ot",
    __name__,
    url_prefix="/costos-ot",
    template_folder="templates",
)


def _contexto(codigo_cli: str) -> dict:
    """Datos compartidos por la vista full y el partial."""
    rows = service.costos_por_cliente(codigo_cli)
    total_kg = sum(r.kg for r in rows)
    total_costo = sum(r.costo_total for r in rows)
    # costo_promedio_kg defendido: si no hay kg, 0 para no dividir por cero.
    costo_promedio_kg = (total_costo / total_kg) if total_kg > 0 else 0.0
    return {
        "codigo_cli": codigo_cli,
        "rows": [r.to_dict() | {"kg": r.kg, "costo_kg": r.costo_kg,
                                "costo_total": r.costo_total,
                                "fecha_cierre": r.fecha_cierre} for r in rows],
        "total_kg": total_kg,
        "total_costo": total_costo,
        "costo_promedio_kg": costo_promedio_kg,
        "n_ordenes": len(rows),
        "fuente": service.fuente(),
        "disponible": service.disponible(),
    }


@costos_ot_bp.route("/cliente/<codigo_cli>")
@requiere_login
@requiere_permiso("cartera.ver")
def por_cliente(codigo_cli: str):
    ctx = _contexto(codigo_cli.strip().upper())
    return render_template("costos_ot/lista.html", **ctx)


@costos_ot_bp.route("/cliente/<codigo_cli>/fragment")
@requiere_login
@requiere_permiso("cartera.ver")
def por_cliente_fragment(codigo_cli: str):
    """Partial HTMX — se embebe en detalle de factura o estado de cuenta."""
    ctx = _contexto(codigo_cli.strip().upper())
    return render_template("costos_ot/fragment.html", **ctx)
