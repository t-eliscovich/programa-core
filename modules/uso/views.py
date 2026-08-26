"""Uso de la app — cuánto la usa cada vendedor y qué hace adentro.

TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
aplicación? ¿y qué movimientos hace?"*.

Dos pantallas:

* `/uso` — la tabla con los seis vendedores, los que no entraron incluidos.
* `/uso/<usuario>` — el detalle de uno: día por día, a qué clientes les abrió
  la ficha, y la lista de todo lo que miró y lo que cambió.

Permiso: `bitacora.ver`, el mismo que la auditoría (Accionista y
Administrador). Es data sobre cómo trabaja una persona: no la ve cualquiera, y
no la ven los vendedores entre ellos —el `scope_vendedor` ya les cierra todo lo
que no sea /mi-cartera, así que esta ruta les da 404 sin escribir nada—.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec

from . import queries
from .registro import es_papel, nombre_de

_LOG = logging.getLogger("programa_core.uso")

uso_bp = Blueprint("uso", __name__, template_folder="templates")

#: Cuánto se mira por defecto.
DIAS_POR_DEFECTO = 30

#: En el CSV la HORA importa (a qué hora del día trabaja cada uno), y el
#: formateador por defecto de `exports` deja sólo la fecha.
def _cuando(valor) -> str:
    return valor.strftime("%d/%m/%Y %H:%M") if valor else ""


#: Lo que dice la columna `tipo` de los movimientos, en castellano.
QUE_HIZO = {"miro": "miró", "hizo": "cambió"}


def _rango() -> tuple[date, date]:
    """El rango elegido, o el último mes."""
    hoy = today_ec()
    desde = _fecha(request.args.get("desde")) or (hoy - timedelta(days=DIAS_POR_DEFECTO - 1))
    hasta = _fecha(request.args.get("hasta")) or hoy
    if desde > hasta:
        desde, hasta = hasta, desde
    return desde, hasta


def _fecha(texto: str | None) -> date | None:
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return None


@uso_bp.route("/uso")
@requiere_login
@requiere_permiso("bitacora.ver")
def lista():
    desde, hasta = _rango()
    error = None
    try:
        filas = queries.resumen(desde, hasta)
        top = queries.pantallas(desde, hasta)
    except Exception as e:  # noqa: BLE001 — la tabla puede no existir todavía
        _LOG.exception("uso.resumen() falló: %s", e)
        filas, top, error = [], [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("vend", "Vendedor"),
                ("usuario", "Usuario"),
                ("dias", "Días"),
                ("entradas", "Veces que entró"),
                ("visitas", "Pantallas"),
                ("clientes", "Clientes"),
                ("papeles", "Impresiones"),
                ("movimientos", "Movimientos"),
                ("celular", "Pantallas del teléfono"),
                ("ultima", "Última vez", _cuando),
            ],
            filename="uso-vendedores.csv",
        )

    return render_template(
        "uso/lista.html",
        filas=filas, top=top, error=error,
        desde=desde, hasta=hasta,
        dias=(hasta - desde).days + 1,
        nombre_de=nombre_de,
    )


@uso_bp.route("/uso/<usuario>")
@requiere_login
@requiere_permiso("bitacora.ver")
def detalle(usuario: str):
    desde, hasta = _rango()
    error = None
    try:
        dias = queries.por_dia(usuario, desde, hasta)
        fichas = queries.clientes(usuario, desde, hasta)
        movs = queries.movimientos(usuario, desde, hasta)
        top = queries.pantallas(desde, hasta, usuario=usuario)
    except Exception as e:  # noqa: BLE001
        _LOG.exception("uso.detalle(%s) falló: %s", usuario, e)
        dias, fichas, movs, top, error = [], [], [], [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            [{**m, "pantalla": nombre_de(m.get("pantalla")),
              "tipo": QUE_HIZO.get(m.get("tipo"), m.get("tipo"))} for m in movs],
            columnas=[
                ("cuando", "Cuándo", _cuando),
                ("tipo", "Qué"),
                ("pantalla", "Pantalla"),
                ("codigo_cli", "Cliente"),
                ("detalle", "Detalle"),
                ("ruta", "Ruta"),
            ],
            filename=f"uso-{usuario}.csv",
        )

    return render_template(
        "uso/detalle.html",
        usuario=usuario, dias=dias, fichas=fichas, movs=movs, top=top,
        error=error, desde=desde, hasta=hasta,
        nombre_de=nombre_de, es_papel=es_papel,
    )
