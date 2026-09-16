"""Uso de la app — cuánto la usa cada vendedor, cada cliente y cada quien en
Intela, y qué hace adentro.

TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
aplicación? ¿y qué movimientos hace?"*. TMT 2026-09-14: *"hace que uso haya
tabs, vendedores, clientes, intela"* — un tercer mundo (la oficina) al lado
de los dos que ya había, cada uno en su pestaña para no mezclarlos.

Dos pantallas:

* `/uso` — tres pestañas (`?tab=vendedores|clientes|intela`), cada una con su
  resumen por persona y su «pantallas más abiertas». Vendedores es la que
  abre por default: es la que se preguntó primero.
* `/uso/<usuario>` — el detalle de uno (vendedor u oficina): día por día, a
  qué clientes les abrió la ficha, y la lista de todo lo que miró y lo que
  cambió.

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

#: Las tres pestañas de `/uso`, en el orden en que se muestran. Vendedores
#: primero porque es la que se preguntó primero (26/08); una `tab` que no
#: está acá cae en la primera, no en un 404 — es sólo una pestaña de más.
TABS = ("vendedores", "clientes", "intela")

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


def _tab() -> str:
    """La pestaña pedida, o «vendedores» si no vino o vino una que no existe."""
    t = (request.args.get("tab") or "").strip().lower()
    return t if t in TABS else "vendedores"


#: Las columnas del CSV, una lista por pestaña — mismo patrón que ya usaba
#: vendedores, nada más que ahora hay tres.
_CSV_VENDEDORES = [
    ("vend", "Vendedor"),
    ("usuario", "Usuario"),
    ("dias", "Días"),
    ("entradas", "Veces que entró"),
    ("visitas", "Pantallas"),
    ("clientes", "Clientes"),
    ("cartera", "Clientes de su cartera"),
    ("papeles", "Impresiones"),
    ("movimientos", "Movimientos"),
    ("celular", "Pantallas del teléfono"),
    ("ultima", "Última vez", _cuando),
]
_CSV_CLIENTES = [
    ("codigo_cli", "Cliente"),
    ("nombre", "Nombre"),
    ("vend", "Vendedor"),
    ("dias", "Días"),
    ("entradas", "Veces que entró"),
    ("visitas", "Pantallas"),
    ("papeles", "Impresiones"),
    ("celular", "Pantallas del teléfono"),
    ("ultima", "Última vez", _cuando),
]
_CSV_INTELA = [
    ("usuario", "Usuario"),
    ("rol", "Rol"),
    ("dias", "Días"),
    ("entradas", "Veces que entró"),
    ("visitas", "Pantallas"),
    ("fichas", "Fichas de cliente"),
    ("movimientos", "Movimientos"),
    ("celular", "Pantallas del teléfono"),
    ("ultima", "Última vez", _cuando),
]


def _suma(filas: list[dict], campo: str) -> int:
    """La columna sumada. Las tablas son de pocas decenas de filas: sumar acá
    y no en la consulta deja el total y el detalle leyendo LA MISMA data —
    si alguna vez no cierran, es que uno de los dos está mal."""
    return sum(int(f.get(campo) or 0) for f in filas)


def _porcentaje(parte: int, total: int) -> str:
    """«12%» o «—» cuando no hay de qué sacarlo."""
    return f"{round(100 * parte / total)}%" if total else "—"


def _tarjetas_vendedores(filas: list[dict]) -> list[dict]:
    entraron = sum(1 for f in filas if (f.get("visitas") or 0) > 0)
    visitas = _suma(filas, "visitas")
    return [
        {"label": "Entraron", "valor": entraron,
         "extra": f"de {len(filas)} vendedores"},
        {"label": "Veces que entraron", "valor": _suma(filas, "entradas")},
        {"label": "Pantallas", "valor": visitas,
         "extra": f"{_suma(filas, 'papeles')} impresiones"},
        {"label": "Clientes abiertos", "valor": _suma(filas, "clientes"),
         "extra": f"de {_suma(filas, 'cartera')} de cartera"},
        {"label": "Cambios", "valor": _suma(filas, "movimientos")},
        {"label": "Del teléfono",
         "valor": _porcentaje(_suma(filas, "celular"), visitas),
         "extra": "de las pantallas"},
    ]


def _tarjetas_clientes(clientes: list[dict], totales: dict) -> list[dict]:
    """TMT 2026-09-16 (dueña): *«podemos sumar arriba total clientes que
    entraron»*. El número solo no se lee —¿60 es mucho?—, así que va con su
    denominador y con los que NUNCA entraron, que es el que se mueve: todo el
    que entra hoy entra por primera vez —el portal tiene tres semanas—, así
    que una tarjeta de «primera vez» repetiría el mismo número."""
    visitas = _suma(clientes, "visitas")
    con_saldo = int(totales.get("con_saldo") or 0)
    return [
        {"label": "Clientes que entraron", "valor": len(clientes),
         "extra": (f"de {con_saldo} con saldo · "
                   f"{_porcentaje(len(clientes), con_saldo)}") if con_saldo else ""},
        {"label": "Nunca entraron", "valor": int(totales.get("nunca") or 0),
         "extra": "desde que arrancó el portal"},
        {"label": "Veces que entraron", "valor": _suma(clientes, "entradas")},
        {"label": "Pantallas", "valor": visitas,
         "extra": f"{_suma(clientes, 'papeles')} impresiones"},
        {"label": "Del teléfono",
         "valor": _porcentaje(_suma(clientes, "celular"), visitas),
         "extra": "de las pantallas"},
    ]


def _tarjetas_intela(intela: list[dict]) -> list[dict]:
    entraron = sum(1 for i in intela if (i.get("visitas") or 0) > 0)
    visitas = _suma(intela, "visitas")
    return [
        {"label": "Entraron", "valor": entraron,
         "extra": f"de {len(intela)} personas"},
        {"label": "Veces que entraron", "valor": _suma(intela, "entradas")},
        {"label": "Pantallas", "valor": visitas},
        {"label": "Fichas de cliente", "valor": _suma(intela, "fichas")},
        {"label": "Cambios", "valor": _suma(intela, "movimientos")},
        {"label": "Del teléfono",
         "valor": _porcentaje(_suma(intela, "celular"), visitas),
         "extra": "de las pantallas"},
    ]


@uso_bp.route("/uso")
@requiere_login
@requiere_permiso("bitacora.ver")
def lista():
    desde, hasta = _rango()
    tab = _tab()
    error = None
    filas, top = [], []
    clientes, top_clientes = [], []
    intela, top_intela = [], []
    tarjetas: list[dict] = []
    try:
        if tab == "clientes":
            clientes = queries.resumen_clientes(desde, hasta)
            top_clientes = queries.pantallas(desde, hasta, ambito="clientes")
            tarjetas = _tarjetas_clientes(clientes, queries.totales_clientes())
        elif tab == "intela":
            intela = queries.resumen_intela(desde, hasta)
            top_intela = queries.pantallas(desde, hasta, ambito="intela")
            tarjetas = _tarjetas_intela(intela)
        else:
            filas = queries.resumen(desde, hasta)
            top = queries.pantallas(desde, hasta, ambito="vendedor")
            tarjetas = _tarjetas_vendedores(filas)
    except Exception as e:  # noqa: BLE001 — la tabla puede no existir todavía
        _LOG.exception("uso.resumen() falló (tab=%s): %s", tab, e)
        error = str(e)

    if request.args.get("export") == "csv":
        datos, columnas, archivo = {
            "vendedores": (filas, _CSV_VENDEDORES, "uso-vendedores.csv"),
            "clientes": (clientes, _CSV_CLIENTES, "uso-clientes.csv"),
            "intela": (intela, _CSV_INTELA, "uso-intela.csv"),
        }[tab]
        return csv_response(datos, columnas=columnas, filename=archivo)

    return render_template(
        "uso/lista.html",
        tab=tab,
        tarjetas=tarjetas,
        filas=filas, top=top,
        clientes=clientes, top_clientes=top_clientes,
        intela=intela, top_intela=top_intela,
        error=error,
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
        # Los de su cartera que NO miró. Es la mitad accionable del informe:
        # sin esto, «abrió 12 clientes» no lleva a ninguna conversación.
        vend = queries.vend_de(usuario)
        sin_abrir = queries.no_abiertos(vend, usuario, desde, hasta) if vend else []
    except Exception as e:  # noqa: BLE001
        _LOG.exception("uso.detalle(%s) falló: %s", usuario, e)
        dias, fichas, movs, top, error = [], [], [], [], str(e)
        vend, sin_abrir = "", []

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
        vend=vend, sin_abrir=sin_abrir,
        error=error, desde=desde, hasta=hasta,
        nombre_de=nombre_de, es_papel=es_papel,
    )
