"""Portal del VENDEDOR — /mi-cartera. Sólo lectura + impresión.

TMT 2026-08-03 (dueña): "Cada vendedor hay que ser un usuario por vendedor, y
tienen que tener acceso solo a sus clientes, sus facturas, sus cheques" +
"vendedores casi siempre usan celular".

Diseño
------
Módulo aparte y no un filtro sobre /facturas, /cheques y /informes por dos
razones: (1) esas pantallas son tablas de escritorio de 12 columnas, y (2)
filtrarlas sería fail-open — ver el docstring de scope_vendedor.py.

El código de vendedor sale SIEMPRE de `g.user["vend"]`, nunca del querystring.
La única excepción es el PREVIEW para los dueños (`?vend=PPR` con permiso
wildcard), que existe para poder mirar lo que ve un vendedor sin pedirle la
contraseña — y que un vendedor no puede usar porque su propio `vend` gana
siempre.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso, tiene_permiso
from filters import today_ec
from modules.informes import queries as informes_queries
from parsers import parse_int
from scope_vendedor import vendedor_de

from . import queries

mi_cartera_bp = Blueprint("mi_cartera", __name__, template_folder="templates")

PERIODOS = ("semana", "mes", "anio")


def _vend_actual() -> str:
    """Código de vendedor del request. 404 si el usuario no es vendedor.

    Un vendedor real SIEMPRE usa el suyo. Los dueños (wildcard) pueden pasar
    `?vend=` para previsualizar; cualquier otro usuario recibe 404, igual que
    con `requiere_permiso`.
    """
    propio = vendedor_de(g.get("user"))
    if propio:
        return propio
    if tiene_permiso("*"):
        pedido = (request.args.get("vend") or "").strip().upper()
        if pedido:
            return pedido
    abort(404)
    return ""  # pragma: no cover - abort() corta


def _periodo() -> str:
    p = (request.args.get("periodo") or "mes").strip().lower()
    return p if p in PERIODOS else "mes"


def _ctx_base(vend: str) -> dict:
    """Lo que necesita el cascarón (nombre, iniciales, link de preview)."""
    nombre = queries.nombre_vendedor(vend)
    partes = [p for p in nombre.replace(".", " ").split() if p]
    iniciales = (partes[0][0] + (partes[1][0] if len(partes) > 1 else "")).upper() \
        if partes else vend[:2].upper()
    return {
        "vend": vend,
        "vend_nombre": nombre,
        "iniciales": iniciales,
        # Los dueños navegan en modo preview: hay que arrastrar ?vend= en
        # cada link o el segundo click los saca del preview.
        "preview": not vendedor_de(g.get("user")),
    }


@mi_cartera_bp.route("/mi-cartera")
@requiere_login
@requiere_permiso("micartera.ver")
def inicio():
    vend = _vend_actual()
    hoy = today_ec()
    periodo = _periodo()
    desde, hasta, etiqueta = queries.rango_periodo(periodo, hoy)

    vendido = queries.ventas(vend, desde, hasta)
    meta = queries.meta_periodo(vend, periodo, hoy)
    esperado = queries.avance_esperado(desde, hasta, hoy)

    # Barritas: las semanas del mes en curso (en 'anio' no entran 52 barras en
    # un celular, así que ahí no se muestran).
    barras = []
    if periodo != "anio":
        d_mes, h_mes, _ = queries.rango_periodo("mes", hoy)
        semanas = queries.ventas_por_semana(vend, d_mes, h_mes)
        tope = max([s["total"] for s in semanas] or [0]) or 1
        lunes_actual = hoy - timedelta(days=hoy.weekday())
        barras = [
            {
                "rotulo": f"S{i + 1}",
                "pct": max(2, round(s["total"] * 100 / tope)),
                "actual": s["semana"] == lunes_actual,
                "total": s["total"],
            }
            for i, s in enumerate(semanas)
        ]

    pend = queries.por_cobrar(vend)
    return render_template(
        "mi_cartera/inicio.html",
        periodo=periodo,
        etiqueta=etiqueta,
        vendido=vendido,
        meta=meta,
        pct_meta=(round(vendido * 100 / meta) if meta else None),
        pct_esperado=round(esperado * 100),
        delta_ritmo=(vendido - meta * esperado) if meta else None,
        cobrado=queries.cobrado(vend, desde, hasta),
        pendiente=pend,
        barras=barras,
        alertas=[c for c in queries.mis_clientes(vend) if c["vencido"] > 0][:5],
        comision=queries.comision(vend, desde, hasta),
        **_ctx_base(vend),
    )


@mi_cartera_bp.route("/mi-cartera/clientes")
@requiere_login
@requiere_permiso("micartera.ver")
def clientes():
    vend = _vend_actual()
    filtro = (request.args.get("f") or "saldo").strip().lower()
    q = (request.args.get("q") or "").strip().lower()

    filas = queries.mis_clientes(vend)
    if filtro == "vencidos":
        filas = [c for c in filas if c["vencido"] > 0]
    if q:
        filas = [
            c for c in filas
            if q in (c["nombre"] or "").lower() or q in (c["codigo_cli"] or "").lower()
        ]

    return render_template(
        "mi_cartera/clientes.html",
        filas=filas,
        filtro=filtro,
        q=request.args.get("q") or "",
        total=sum(c["saldo"] for c in filas),
        **_ctx_base(vend),
    )


def _cargar_cliente(vend: str, codigo_cli: str) -> dict:
    """Estado de cuenta de un cliente PROPIO. 404 si no lo es.

    El guard va ANTES de tocar la base: sin él, tipear el código de un cliente
    ajeno en la barra de direcciones sería una fuga directa.
    """
    if not queries.cliente_es_mio(vend, codigo_cli):
        abort(404)
    data = informes_queries.estado_cuenta_cliente(codigo_cli)
    if not data.get("cliente"):
        abort(404)
    # La dueña 2026-08-03: el vendedor NO ve el cupo del cliente.
    data["cliente"].pop("cupo", None)
    return data


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>")
@requiere_login
@requiere_permiso("micartera.ver")
def cliente(codigo_cli: str):
    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    return render_template(
        "mi_cartera/cliente.html",
        hoy=today_ec(),
        tab=(request.args.get("tab") or "facturas").strip().lower(),
        **data,
        **_ctx_base(vend),
    )


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/imprimir")
@requiere_login
@requiere_permiso("micartera.ver")
def imprimir(codigo_cli: str):
    """Versión para imprimir / guardar como PDF y mandársela al cliente."""
    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    return render_template(
        "mi_cartera/imprimir.html", hoy=today_ec(), **data, **_ctx_base(vend)
    )


@mi_cartera_bp.route("/mi-cartera/comision")
@requiere_login
@requiere_permiso("micartera.ver")
def comision():
    vend = _vend_actual()
    hoy = today_ec()
    periodo = _periodo()
    desde, hasta, etiqueta = queries.rango_periodo(periodo, hoy)
    return render_template(
        "mi_cartera/comision.html",
        periodo=periodo,
        etiqueta=etiqueta,
        monto=queries.comision(vend, desde, hasta),
        meses=queries.comision_meses(vend, hoy.year, hoy.month),
        vendido_anio=queries.ventas(vend, hoy.replace(month=1, day=1), hoy),
        meta_anio=queries.meta_anio(vend, hoy.year),
        anio=hoy.year,
        **_ctx_base(vend),
    )


# ---------------------------------------------------------------------------
# Carga de metas — pantalla de la DUEÑA, no del vendedor.
# ---------------------------------------------------------------------------
# Vive fuera de /mi-cartera a propósito: scope_vendedor.py sólo deja pasar
# /mi-cartera, así que un vendedor no puede llegar acá ni tipeando la URL.


@mi_cartera_bp.route("/vendedores/metas", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("metas.editar")
def metas():
    hoy = today_ec()
    anio = parse_int(request.args.get("anio")) or hoy.year

    if request.method == "POST":
        anio = parse_int(request.form.get("anio")) or hoy.year
        usuario = (g.get("user") or {}).get("username") or "web"
        for clave, valor in request.form.items():
            if not clave.startswith("meta_"):
                continue
            _, codigo, mes = clave.split("_", 2)
            queries.guardar_meta(codigo, anio, int(mes), (valor or "").strip() or None,
                                 usuario)
        return redirect(url_for("mi_cartera.metas", anio=anio))

    vendedores = queries.vendedores_activos()
    cargadas = {(m["codigo"], int(m["mes"])): m["monto"] for m in queries.metas_del_anio(anio)}
    return render_template(
        "mi_cartera/metas.html", anio=anio, vendedores=vendedores, cargadas=cargadas,
        hoy=hoy,
    )
