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

import calendar
from datetime import date, timedelta

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso, tiene_permiso
from filters import today_ec
from modules.informes import queries as informes_queries
from modules.informes import views as informes_views
from parsers import parse_int
from scope_vendedor import vendedor_de

from . import queries

mi_cartera_bp = Blueprint("mi_cartera", __name__, template_folder="templates")

PERIODOS = ("semana", "mes", "anio")

MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")


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


def _anio_vs_meta(vend: str, hoy) -> dict:
    """Los KILOS vendidos y la meta del año, comparables entre sí.

    Si la dueña cargó sólo algunos meses, se comparan ESOS meses contra esas
    metas. Comparar el año entero contra media meta daba 3345% (2026-08-03).
    """
    meta = queries.meta_anio(vend, hoy.year)
    if not meta:
        return {"vendido_anio": queries.ventas_kg(vend, hoy.replace(month=1, day=1), hoy),
                "meta_anio": None, "nota_anio": ""}
    meses = queries.meses_con_meta(vend, hoy.year)
    if len(meses) >= 12:
        return {"vendido_anio": queries.ventas_kg(vend, hoy.replace(month=1, day=1), hoy),
                "meta_anio": meta, "nota_anio": ""}
    return {
        "vendido_anio": queries.ventas_kg_en_meses(vend, hoy.year, meses),
        "meta_anio": meta,
        "nota_anio": (f"{len(meses)} mes cargado" if len(meses) == 1
                      else f"{len(meses)} meses cargados"),
    }


@mi_cartera_bp.route("/mi-cartera")
@requiere_login
@requiere_permiso("micartera.ver")
def inicio():
    vend = _vend_actual()
    hoy = today_ec()
    periodo = _periodo()
    desde, hasta, etiqueta = queries.rango_periodo(periodo, hoy)

    # ⭐ TODO lo de esta tarjeta va en KILOS — lo vendido, la meta, las barras
    # y el ritmo. Dueña 2026-08-05: *"las metas se mide en kilos"*. Mezclar
    # unidades acá sería peor que no tenerlas: el % del anillo saldría de
    # dividir kilos por dólares.
    vendido = queries.ventas_kg(vend, desde, hasta)
    meta = queries.meta_periodo(vend, periodo, hoy)
    esperado = queries.avance_esperado(desde, hasta, hoy)
    nota_meta = ""

    # ⚠ La meta del AÑO es la suma de los meses CARGADOS, no una de 12 meses.
    # Comparada contra las ventas del año entero da un disparate: el
    # 2026-08-03, con sólo agosto cargado ($10.000) y $334.524 vendidos en el
    # año, el anillo marcaba 3345%. Se compara like con like — lo vendido en
    # los meses que tienen meta contra la suma de esas metas — y se aclara
    # sobre cuántos meses habla.
    if periodo == "anio" and meta:
        anio = _anio_vs_meta(vend, hoy)
        vendido = anio["vendido_anio"]
        if anio["nota_anio"]:
            nota_meta = f"sobre {anio['nota_anio']}"
            # El "ritmo" del año no aplica cuando el período no es el año
            # entero: sin los 12 meses no se sabe cuánto debería llevar.
            esperado = None

    # Barritas: las semanas del mes en curso (en 'anio' no entran 52 barras en
    # un celular, así que ahí no se muestran).
    barras = []
    if periodo != "anio":
        d_mes, h_mes, _ = queries.rango_periodo("mes", hoy)
        semanas = queries.ventas_kg_por_semana(vend, d_mes, h_mes)
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
        # Una sola barra no es un gráfico: es un rectángulo de color. Las
        # barras existen para COMPARAR semanas entre sí; con una sola (los
        # primeros días del mes) no hay nada que comparar y encima ocupa el
        # lugar donde el vendedor espera información. Visto a 390 px el 03/08.
        if len(barras) < 2:
            barras = []

    pend = queries.por_cobrar(vend)
    return render_template(
        "mi_cartera/inicio.html",
        periodo=periodo,
        etiqueta=etiqueta,
        vendido=vendido,
        meta=meta,
        pct_meta=(round(vendido * 100 / meta) if meta else None),
        pct_esperado=(round(esperado * 100) if esperado is not None else None),
        delta_ritmo=((vendido - meta * esperado)
                     if (meta and esperado is not None) else None),
        nota_meta=nota_meta,
        # El ritmo se mide en días HÁBILES: la fábrica no factura sábados ni
        # domingos (0 facturas de domingo desde que hay datos). Se muestra el
        # denominador porque un "abajo del ritmo" que no dice contra qué es
        # exactamente lo que la dueña no pudo verificar el 04/08 — y tenía
        # razón, estaba mal.
        nota_ritmo=(f"día hábil {queries.dias_habiles(desde, min(hoy, hasta))}"
                    f" de {queries.dias_habiles(desde, hasta)}"
                    if (meta and esperado is not None) else None),
        cobrado=queries.cobrado(vend, desde, hasta),
        pendiente=pend,
        barras=barras,
        # Las 5 alertas son las de MAYOR vencido. El orden se pide acá y no
        # se hereda del ORDER BY de la query: la lista de Mis clientes la
        # ordena alfabéticamente y, si esto colgara del orden de la query,
        # el Inicio habría pasado a mostrar cinco vencidos cualesquiera sin
        # que se rompiera nada.
        alertas=sorted(
            (c for c in queries.mis_clientes(vend) if c["vencido"] > 0),
            key=lambda c: float(c.get("vencido") or 0),
            reverse=True,
        )[:5],
        # Para el mes se usa la MISMA cuenta que la pantalla de comisión
        # (suma del desglose). Si el Inicio dijera 7,73 y Comisión 7,74, el
        # vendedor no sabe a cuál creerle.
        comision=(queries.comision_mes(vend, hoy.year, hoy.month)
                  if periodo == "mes"
                  else queries.comision(vend, desde, hasta)),
        **_ctx_base(vend),
    )


@mi_cartera_bp.route("/mi-cartera/clientes")
@requiere_login
@requiere_permiso("micartera.ver")
def clientes():
    vend = _vend_actual()
    filtro = (request.args.get("f") or "saldo").strip().lower()
    q = (request.args.get("q") or "").strip().lower()

    # TMT 2026-08-11 (dueña): *"en la app de los vendedores también puedes
    # habilitar que se ordene alfabéticamente"* + *"ordena por codigo todo"*.
    #
    # Por CÓDIGO, no por nombre: es el mismo orden con el que sale la hoja
    # impresa —la de la oficina desde el 04/08— y el vendedor tiene la lista
    # en el celular y el papel en la mano al mismo tiempo. Dos alfabéticos
    # distintos en las dos superficies del mismo dato es peor que uno solo,
    # aunque en pantalla el código se lea chiquito.
    #
    # Con 94 clientes, "lo que más debe primero" servía para mirar la cartera,
    # no para encontrar a alguien — que es a lo que se entra a esta pantalla.
    # El vencido no se pierde: sigue el tag rojo, el filtro Vencidos y las
    # alertas del Inicio, que sí van por monto.
    filas = sorted(
        queries.mis_clientes(vend),
        key=lambda c: (c.get("codigo_cli") or "").upper(),
    )
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

    ⚖️ EL CUPO SÍ VIAJA — decisión REVERTIDA el 2026-08-05. El 03/08 la dueña
    había dicho que el vendedor no lo viera y acá había un `pop("cupo")` con
    su test. Andrés, por WhatsApp: *"por favor es clave eso, mostrar cupos y
    descuentos del cliente / los vendedores tienen apegarse a los cupos y
    ordenar a los clientes"*, y ella lo aprobó.

    El cambio de opinión tiene sentido: esconder el cupo protege el dato, pero
    el que decide cuánto cargarle a un cliente es el vendedor, parado en el
    local. Si no lo ve, "apegarse al cupo" es una instrucción sin instrumento.

    ⚠ Lo que sigue abierto: el cupo está cargado en el ~10% de los clientes
    (EDG 29 de 339, JQU 5 de 192, PPR 24 de 235; sólo FL1 llega a 78 de 216).
    Por eso la ficha dice "sin cupo asignado" y NO "$ 0" — un cero se lee como
    "no puede comprar nada", que es lo contrario de lo que pasa.
    """
    if not queries.cliente_es_mio(vend, codigo_cli):
        abort(404)
    data = informes_queries.estado_cuenta_cliente(codigo_cli)
    if not data.get("cliente"):
        abort(404)
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
    """Impresión de UN cliente.

    TMT 2026-08-03 (dueña, mandando el link de
    /informes/estado-cuenta/imprimir?por=vendedor&sel=EDG): *"Imprimir tiene
    que imprimir lo mismo que acá"*. Así que NO se arma una hoja propia: se
    renderiza EXACTAMENTE el mismo template que la impresión de la oficina
    (`informes/estado_cuenta_lote_print.html`, que a su vez incluye el
    parcial `_estado_cuenta_impreso.html`).

    Dos plantillas distintas divergen a la primera corrección que se le hace
    a una sola — y el papel que el vendedor le deja al cliente tiene que ser
    el mismo que sale de la oficina.

    El parcial es READ-ONLY salvo que se le pase `interactivo` (los dropdowns
    Z/A/T/X cuelgan de ese flag); acá no se pasa, y además el vendedor no
    tiene facturas.ver / clientes.ver.
    """
    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    return render_template(
        "informes/estado_cuenta_lote_print.html",
        clientes=[data],
        titulo=f"{data['cliente']['nombre']} ({codigo_cli})",
        por="vendedor",
        n=1,
    )


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/pdf")
@requiere_login
@requiere_permiso("micartera.ver")
def pdf(codigo_cli: str):
    """El estado de cuenta como archivo, para mandárselo al cliente.

    Es EL MISMO PDF que genera la oficina desde
    /informes/estado-cuenta/<cod>/pdf — misma función, mismo template, mismo
    archivo. Lo único propio de acá es el guard: `_cargar_cliente` 404ea si el
    cliente no es de este vendedor.

    (El vendedor no puede llegar a la ruta de informes: scope_vendedor.py sólo
    deja pasar /mi-cartera. Por eso hay dos puertas y una sola cocina.)
    """
    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    return informes_views.responder_pdf(data, (codigo_cli or "").upper())


@mi_cartera_bp.route("/mi-cartera/imprimir")
@requiere_login
@requiere_permiso("micartera.ver")
def imprimir_todos():
    """Impresión de TODA la cartera del vendedor, un cliente tras otro.

    Es el equivalente exacto de
    /informes/estado-cuenta/imprimir?por=vendedor&sel=<código> — mismo
    template, MISMO ORDEN, mismo cuerpo por cliente. La diferencia es que el
    vendedor no elige el `sel`: sale de su sesión.

    ⚠ El orden es ALFABÉTICO POR CÓDIGO porque así imprime la oficina desde el
    2026-08-04 (pedido de Alex: *"al momento de imprimir vuelve a detectar el
    orden descendente"*). Acá había quedado por saldo descendente: las dos
    rutas rendean la misma hoja y venían saliendo en orden distinto, que es
    justo lo que "imprimir tiene que imprimir lo mismo que acá" no quiere.
    Por CÓDIGO, igual que la lista de la pantalla: el vendedor tiene las dos
    cosas delante a la vez.
    """
    vend = _vend_actual()
    filas = sorted(
        queries.mis_clientes(vend),
        key=lambda r: (r.get("codigo_cli") or "").upper(),
    )
    clientes = []
    for f in filas:
        d = informes_queries.estado_cuenta_cliente(f["codigo_cli"])
        if d and d.get("cliente"):
            # El cupo ya no se saca — ver `_cargar_cliente`. (La hoja impresa
            # es la de la oficina y no lo dibuja, pero el dato viaja igual y
            # tiene que hacerlo por el mismo camino en las dos rutas.)
            clientes.append(d)
    return render_template(
        "informes/estado_cuenta_lote_print.html",
        clientes=clientes,
        titulo=f"Vendedor: {queries.nombre_vendedor(vend)}",
        por="vendedor",
        n=len(clientes),
    )


@mi_cartera_bp.route("/mi-cartera/comision")
@requiere_login
@requiere_permiso("micartera.ver")
def comision():
    """Mi comisión — SÓLO MENSUAL, con el detalle de qué la generó.

    TMT 2026-08-03 (dueña): *"comisión solo necesito mensual, y podés mostrar
    cada mes, seguro quieren saber de qué clientes están ganando esta
    comisión, que la comisión diga de qué cobranza es"*.

    Se fue el selector Semana/Mes/Año (una comisión semanal no se paga, así
    que el número no significaba nada) y entró la navegación mes a mes más el
    desglose: por cliente, y dentro de cada cliente, cobro por cobro.

    ⚠ Mostrar el cobrado al lado de la comisión deja despejar el %, que en
    agosto se había decidido no mostrar. Es consecuencia inevitable de
    "que diga de qué cobranza es", y es un pedido posterior y más específico.
    """
    vend = _vend_actual()
    hoy = today_ec()
    anio = parse_int(request.args.get("anio")) or hoy.year
    mes = parse_int(request.args.get("mes")) or hoy.month
    # Nunca un mes futuro: la comisión de un mes que no pasó es 0 y confunde.
    if (anio, mes) > (hoy.year, hoy.month):
        anio, mes = hoy.year, hoy.month
    mes = max(1, min(12, mes))

    ultimo = calendar.monthrange(anio, mes)[1]
    grupos = queries.comision_por_cliente(vend, anio, mes)

    ant = date(anio, mes, 1) - timedelta(days=1)
    sig = date(anio, mes, ultimo) + timedelta(days=1)

    # El mes a mes quedaba al pie, debajo del desglose de cobranzas — que es
    # tan largo como clientes le cobró el vendedor. Dueña 2026-08-05: *"este
    # mes a mes está muy abajo, pongamos arriba otra tab o algo así"*. Sube a
    # una pestaña, con el MISMO control segmentado que Semana/Mes/Año del
    # Inicio (ella mandó esa captura como referencia): un componente que ya
    # sabe usar, en vez de un patrón nuevo para lo mismo.
    tab = "meses" if (request.args.get("tab") or "").strip().lower() == "meses" else "mes"

    # Los meses SIN comisión no se listan. Enero a mayo daban cinco filas de
    # "$ 0,00" que ocupaban media pantalla y empujaban abajo los meses que sí
    # tienen plata. Un cero pide la misma atención que una cifra y no dice
    # nada — mismo criterio que el guión de la columna Abonado.
    meses_todos = queries.comision_meses(vend, hoy.year, hoy.month)
    return render_template(
        "mi_cartera/comision.html",
        tab=tab,
        n_meses_sin_comision=sum(1 for m in meses_todos if not m["monto"]),
        anio=anio,
        mes=mes,
        etiqueta=f"{MESES[mes - 1]} {anio}",
        # El total es la SUMA del desglose, no un redondeo aparte: si no,
        # el encabezado y el detalle se separan por centavos.
        monto=round(sum(g["comision"] for g in grupos), 2),
        cobrado=sum(g["cobrado"] for g in grupos),
        grupos=grupos,
        mes_anterior=(ant.year, ant.month),
        mes_siguiente=((sig.year, sig.month)
                       if (sig.year, sig.month) <= (hoy.year, hoy.month) else None),
        meses=[m for m in meses_todos if m["monto"]],
        es_mes_actual=(anio, mes) == (hoy.year, hoy.month),
        nombres_meses=MESES,
        **_ctx_base(vend),
    )


# ---------------------------------------------------------------------------
# Carga de metas — pantalla de la DUEÑA, no del vendedor.
# ---------------------------------------------------------------------------
# Vive fuera de /mi-cartera a propósito: scope_vendedor.py sólo deja pasar
# /mi-cartera, así que un vendedor no puede llegar acá ni tipeando la URL.
#
# TMT 2026-08-03 (dueña): *"metas por vendedor ponelo en una tab dentro de
# comisiones"*. Cuelga de /comisiones y las dos pantallas comparten la barra
# de pestañas; se sacó la entrada suelta del menú.


@mi_cartera_bp.route("/comisiones/metas", methods=["GET", "POST"])
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
    cargadas = {(m["codigo"], int(m["mes"])): m["kg"] for m in queries.metas_del_anio(anio)}
    # Andrés 2026-08-05: *"le pongas los kilos vendidos reales y el % de
    # cumplimiento de las ventas de cada vendedor"*. Una meta sin el real al
    # lado es un número que nadie vuelve a mirar.
    vendido = queries.ventas_kg_por_vendedor_mes(anio)
    # Los meses que TODAVÍA NO PASARON no llevan real ni %: un 0% en diciembre
    # no dice que el vendedor va mal, dice que diciembre no llegó. Y el mes en
    # curso se marca, porque su % es parcial por definición.
    ultimo_mes = hoy.month if anio == hoy.year else (12 if anio < hoy.year else 0)
    return render_template(
        "mi_cartera/metas.html", anio=anio, vendedores=vendedores, cargadas=cargadas,
        vendido=vendido, ultimo_mes=ultimo_mes, mes_en_curso=(
            hoy.month if anio == hoy.year else None),
        hoy=hoy,
    )
