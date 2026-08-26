"""Sección Análisis — su propio menú, aparte del programa del día a día."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec
from modules._lib import pdf_motor

from . import excel, queries

analisis_bp = Blueprint("analisis", __name__, template_folder="templates")

_LOG = logging.getLogger("programa_core.analisis")

# El menú de la sección. Vive acá y no en el template para que agregar una
# pantalla sea una línea y no un copy-paste de HTML en dos lugares.
# Los 7 códigos que en Programa Core apuntan a DOS empresas distintas
# (migración 0155, lista abierta). Ahí el código no identifica a nadie y hay que
# mirar el RUC antes de llamar.
CODIGOS_AMBIGUOS = {"BLP", "BRC", "JQS", "LEC", "YMO", "JRP", "MTE"}

MENU = [
    {
        "url": "/analisis/parado",
        "titulo": "Saldos",
        "bajada": "Qué está quieto en bodega y a qué cliente llamarle por eso.",
        "listo": True,
    },
    {
        "url": "/analisis/parado/clientes",
        "titulo": "A quién ofrecerle qué",
        "bajada": "La hoja del vendedor: cliente por cliente, qué saldos "
                  "puede llevarse. Se imprime.",
        "listo": True,
    },
    {
        "url": "/analisis/competencia",
        "titulo": "Competencia",
        "bajada": "Cuánto queda de lo parado y quién va ganando. La ven todos, "
                  "vendedores incluidos.",
        "listo": True,
    },
    {
        "url": "/analisis/clientes",
        "titulo": "Clientes",
        "bajada": "Quién se movió, quién dejó de comprar, quién se está enfriando.",
        "listo": False,
    },
]


#: El menú que ve un VENDEDOR. Es otro, y no un subconjunto del de la oficina:
#: sus pantallas son otras rutas (las de /analisis/competencia/…). Mostrarle el
#: de la oficina sería darle tres links que le dan 404.
MENU_VENDEDOR = [
    {"url": "/analisis/competencia", "titulo": "Competencia", "listo": True},
    {"url": "/analisis/competencia/telas", "titulo": "Saldos",
     "listo": True},
    {"url": "/analisis/competencia/mi-hoja", "titulo": "Tu hoja", "listo": True},
]


@analisis_bp.context_processor
def _menu():
    """El menú de arriba lo ve todo template de la sección.

    Sólo las pantallas listas: un link a algo que todavía no existe da 404, y
    en esta app los links son strings hardcodeados — no se ve desde el código.
    """
    if (g.get("user") or {}).get("vend"):
        return {"menu_global": MENU_VENDEDOR}
    return {"menu_global": [m for m in MENU if m["listo"]]}


@analisis_bp.route("/analisis")
@requiere_login
@requiere_permiso("analisis.ver")
def inicio():
    return render_template("analisis/inicio.html", menu=MENU)


@analisis_bp.route("/analisis/parado")
@requiere_login
@requiere_permiso("analisis.ver")
def parado():
    # ⚠ Dos listas y no una. `base` es una fila por tela × color: de ahí salen
    # el resumen de arriba y el cuadro por grupo, que tienen que seguir
    # contando ÍTEMS (734), no renglones. `filas` es lo que se dibuja, abierto
    # por forma y calidad — ver `abrir_en_lineas`.
    base = queries.con_puntos(queries.items())
    filas = queries.abrir_en_lineas(base)
    # ⭐ EL QUE SE VENDIÓ SE QUEDA, TACHADO (dueña 25/08/2026: "¿esto que se
    # vendió por ejemplo? no hay que ponerlo en 0. ¿Tachar la fila y decir
    # vendido?"). Primero se habían sacado los renglones de 0 kg —"los que hay
    # 0 no tienen que estar"— y eso rompía dos cosas: la lista dejaba de mostrar
    # justo lo que la competencia premia, y el click de la tabla de Vendidos,
    # que lleva a la fila de esa tela, no encontraba a dónde ir.
    # Lo que molestaba era el CERO pelado, no la fila. Ahora va tachada y con
    # la palabra vendido.
    # ⚠ El que quedó en cero SIN haber vendido nada sí se va: es un ajuste de
    # bodega, no un logro, y no hay nada que ofrecer ahí.
    filas = [f for f in filas
             if float(f.get("stock_kg") or 0) > 0
             or float(f.get("kg_vendidos") or 0) > 0]
    filas.sort(key=lambda f: -float(f.get("puntos_fila") or 0))
    # Los desplegables salen de las filas que se van a dibujar, no de una lista
    # aparte: si un grupo no tiene nada parado, no tiene por qué estar.
    grupos = sorted({f["categoria"] for f in filas if f["categoria"]})
    subgrupos = sorted(
        {(f["subcategoria"], f["categoria"] or "") for f in filas},
        key=lambda x: x[0])
    return render_template(
        "analisis/parado.html",
        filas=filas,
        grupos=grupos,
        subgrupos=[{"sub": s, "cat": c} for s, c in subgrupos],
        llamados=queries.llamados_por_tela(),
        resumen=queries.resumen(base, queries.kg_al_arrancar()),
        bolsa=queries.bolsa_congelada(),
        grupos_resumen=queries.por_grupo(base),
        estado=queries.estado(),
        # ⭐ La tabla de abajo: qué se vendió, qué día y quién.
        vendidos=queries.vendidos(queries.config("largada", "2026-08-25")),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        ahora_anio=today_ec().year,
    )


@analisis_bp.route("/analisis/parado.xlsx")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_xlsx():
    """La lista entera a Excel, con formato.

    Dueña 26/08/2026: *"bajar a excel se baja horrible: bajalo a algo con
    formato"*. Era un CSV armado en el navegador —punto y coma, todo texto,
    los kilos sin poder sumarse—. Ver `excel.py` para el porqué de bajar todo
    y no lo filtrado.
    """
    # ⚠ Las MISMAS líneas que la pantalla: abiertas por forma y calidad. Bajaba
    # la fila sin abrir, y por eso llevaba «Kg de primera» y «Kg de segunda»
    # como columnas aparte —dos números para una fila que en la pantalla ya son
    # dos renglones— (dueña 26/08/2026: "¿por qué tengo esto, si ya tengo la
    # división entre PRI y SEG?"). Tenía razón: con las líneas abiertas cada
    # renglón tiene UNA categoría y esas dos columnas dejan de decir nada.
    # De paso el archivo y la pantalla cuentan lo mismo: eran 700 contra 709.
    filas = queries.abrir_en_lineas(queries.con_puntos(queries.items()))
    cols = [
        ("Grupo", 12, None), ("Tela", 24, None),
        ("Color", 8, None), ("Nombre del color", 16, None),
        ("Forma", 8, None), ("Categoría", 10, None),
        ("Queda", 11, "#,##0.0"), ("Vendido", 11, "#,##0.0"),
        ("Vale (puntos por kilo)", 12, "#,##0"),
        ("Puntos de la fila", 14, "#,##0"),
        ("Kg al marcarlo", 13, "#,##0.0"),
        ("Clientes que compran esta tela", 16, "#,##0"),
        ("Última venta", 13, "dd/mm/yyyy"), ("Marcado el", 12, "dd/mm/yyyy"),
    ]
    datos = [[
        f.get("categoria"), f.get("subcategoria"), f.get("color"),
        f.get("color_nombre"), _forma(f), _categoria(f),
        _num(f.get("stock_kg")), _num(f.get("kg_vendidos")),
        _num(f.get("puntos")), _num(f.get("puntos_fila")),
        _num(f.get("kg_al_marcar")), _num(f.get("clientes")),
        f.get("ultima_venta"), f.get("fecha_marcado"),
    ] for f in filas]
    hoy = today_ec()
    return excel.respuesta(
        f"saldos_{hoy:%Y_%m_%d}.xlsx",
        excel.libro([{"titulo": "Saldos", "columnas": cols, "filas": datos}]))


@analisis_bp.route("/analisis/vendidos.xlsx")
@requiere_login
@requiere_permiso("analisis.ver")
def vendidos_xlsx():
    """Lo vendido desde la largada, renglón por renglón, con formato.

    Dueña 26/08/2026: *"y lo mismo con los vendidos"*.
    """
    filas = queries.vendidos(queries.config("largada", "2026-08-25"))
    cols = [
        ("Grupo", 12, None), ("Tela", 24, None),
        ("Color", 8, None), ("Nombre del color", 16, None),
        ("Forma", 8, None), ("Categoría", 10, None),
        ("Queda", 11, "#,##0.0"), ("Vendido", 11, "#,##0.0"),
        ("Vale (puntos por kilo)", 12, "#,##0"),
        ("Puntos", 11, "#,##0"),
        ("Vendedor", 20, None), ("Día", 12, "dd/mm/yyyy"),
        ("Factura", 12, "#,##0"),
    ]
    datos = [[
        v.get("categoria"), v.get("subcategoria"), v.get("color"),
        v.get("color_nombre"), v.get("forma_fila"), v.get("calidad"),
        _num(v.get("queda")), _num(v.get("kg")),
        _num(v.get("puntos")), _num(v.get("puntos_fila")),
        v.get("vendedor"), v.get("fecha"), _num(v.get("numf")),
    ] for v in filas]
    hoy = today_ec()
    return excel.respuesta(
        f"vendidos_{hoy:%Y_%m_%d}.xlsx",
        excel.libro([{"titulo": "Vendidos", "columnas": cols, "filas": datos}]))


def _num(v):
    """Un número de verdad, o None. Excel no puede sumar una cadena."""
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return v
    return int(n) if n == int(n) else round(n, 2)


def _forma(f) -> str:
    """TUB o ABI: la de la línea si viene abierta, si no la de la tela."""
    if f.get("forma_fila"):
        return f["forma_fila"]
    if f.get("kg_tubular"):
        return "TUB"
    if f.get("kg_abierta"):
        return "ABI"
    return f.get("forma") or ""


def _categoria(f) -> str:
    """PRI, SEG o las dos: lo mismo que muestra la píldora de la fila."""
    if f.get("cal_fila"):
        return f["cal_fila"]
    pri, seg = f.get("kg_primera") or 0, f.get("kg_segunda") or 0
    if pri and seg:
        return "PRI SEG"
    return "SEG" if seg else "PRI"


@analisis_bp.route("/analisis/parado/clientes")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_clientes():
    """
    La hoja del vendedor: qué telas ofrecerle a qué cliente.

    ⭐ Es la MISMA página en pantalla y en papel. `@media print` esconde el
    cascarón y listo. Dos plantillas para el mismo documento divergen a la
    primera corrección que se le hace a una sola — ya pasó en /mi-cartera, donde
    la oficina y el portal imprimieron órdenes distintos durante ocho días sin
    que nadie lo notara.

    ⭐ Y por eso el ORDEN viaja en la URL: lo que se ve es lo que se imprime.
    """
    vend = (request.args.get("vend") or "").strip().upper() or None
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return render_template(
        "analisis/parado_clientes.html",
        vend=vend, ordenes=queries.ORDENES,
        estado=queries.estado(),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        **queries.por_cliente(vend, orden),
    )


@analisis_bp.route("/analisis/competencia/telas.csv")
@requiere_login
def mis_telas_csv():
    """Las telas paradas a Excel. Sin clientes: es la lista de la fábrica."""
    return csv_response(
        queries.items(),
        columnas=[
            ("categoria", "Grupo"),
            ("subcategoria", "Subgrupo (tela)"),
            ("color", "Color"),
            ("stock_kg", "Kg en saldo"),
            ("pct_total", "% del saldo"),
            ("kg_primera", "Kg de primera"),
            ("kg_segunda", "Kg de segunda"),
            ("ultima_venta", "Última venta"),
        ],
        filename="saldos.csv",
    )


@analisis_bp.route("/analisis/competencia/mi-hoja.csv")
@requiere_login
def mi_hoja_csv():
    """La hoja propia a Excel. Mismo recorte por cartera que la pantalla."""
    vend = _vend_actual()
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return csv_response(
        queries.por_cliente_plano(None, orden, cartera_de=vend),
        columnas=[
            ("tipo", "Candidato o improbable"),
            ("orden_en_la_hoja", "Orden en la hoja"),
            ("codigo", "Código"),
            ("nombre", "Cliente"),
            ("provincia", "Provincia"),
            ("subcategoria", "Tela"),
            ("colores_parados", "Colores en saldo"),
            ("puntos", "Puntos por kilo"),
            ("kg_parado", "Kg en saldo de esa tela"),
            ("kg_cliente", "Kg que le vendimos"),
            ("ultima_compra", "Última compra"),
        ],
        filename=f"mi_hoja{'_' + vend if vend else ''}.csv",
    )


@analisis_bp.route("/analisis/parado/clientes.csv")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_clientes_csv():
    """
    La hoja del vendedor a Excel — una fila por cliente × tela.

    Acá el filtro por vendedor SÍ viaja, porque no es un filtro de JavaScript:
    ya está en la URL de la pantalla y lo aplica la misma función que la dibuja
    (`por_cliente`). El orden también, para que el archivo salga en el mismo
    orden que el papel.
    """
    vend = (request.args.get("vend") or "").strip().upper() or None
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return csv_response(
        queries.por_cliente_plano(vend, orden),
        columnas=[
            ("tipo", "Candidato o improbable"),
            ("orden_en_la_hoja", "Orden en la hoja"),
            ("codigo", "Código"),
            ("nombre", "Cliente"),
            ("provincia", "Provincia"),
            ("vendedor", "Vendedor"),
            ("kg_potencial", "Kg en saldo de telas que compra"),
            ("subcategoria", "Tela"),
            ("colores_parados", "Colores en saldo"),
            ("puntos", "Puntos por kilo"),
            ("kg_parado", "Kg en saldo de esa tela"),
            ("kg_cliente", "Kg que le vendimos"),
            ("ultima_compra", "Última compra"),
            ("anio", "Año"),
        ],
        filename=f"a_quien_ofrecerle_que{'_' + vend if vend else ''}.csv",
    )


@analisis_bp.route("/analisis/competencia")
@requiere_login
def competencia():
    """
    El tablero de la competencia. Dueña 17/08/2026: "aca tienen acceso todos,
    vendedores sobre todo incluidos".

    ⭐ SIN `@requiere_permiso` a propósito: "todos" acá quiere decir cualquiera
    que entre al programa. Es la única ruta de la sección sin gate, y es
    deliberado — no un descuido. Sólo lee y no muestra plata: kilos, metas y
    puestos.

    ⚠ No alcanza con sacar el gate: un usuario VENDEDOR está encerrado en
    /mi-cartera por `scope_vendedor.py` y cualquier otra ruta le da 404. La
    apertura de verdad está allá, en PREFIJOS_PERMITIDOS.
    """
    # ⭐ El bloque "lo tuyo" sale del vendedor del USUARIO LOGUEADO, nunca del
    # querystring: si viniera de la URL, cualquiera vería la cartera de
    # cualquiera cambiando tres letras. Mismo criterio que /mi-cartera.
    vend = _vend_actual()
    datos = queries.competencia()
    return render_template(
        "analisis/competencia.html",
        vend=vend,
        telas=queries.telas_a_sacar(queries.items(), queries.puntos_por_tela()),
        mis_clientes=queries.mis_clientes_parado(vend) if vend else [],
        **datos)


@analisis_bp.route("/analisis/competencia/telas")
@requiere_login
def mis_telas():
    """
    "Lo parado" para el vendedor: LA MISMA pantalla, con sus clientes adentro.

    Dueña 17/08/2026: "la tab de que hay que sacar copiala para ellos. y que
    vean con sus clientes… estaba linda diseñada".

    ⭐ Es el mismo template que /analisis/parado. Lo único que cambia es de
    dónde salen los candidatos —su cartera en vez de todas— y la columna
    "clientes", que pasa a contar los SUYOS: dejarla con el total de la fábrica
    diría "137 clientes" y al abrir la fila aparecerían tres.
    """
    vend = _vend_actual()
    base = queries.con_puntos(queries.items())
    llamados = queries.llamados_por_tela(cartera_de=vend)
    for f in base:
        f["clientes"] = len(llamados.get(f["subcategoria"], []))
    filas = queries.abrir_en_lineas(base)
    # ⭐ EL QUE SE VENDIÓ SE QUEDA, TACHADO (dueña 25/08/2026: "¿esto que se
    # vendió por ejemplo? no hay que ponerlo en 0. ¿Tachar la fila y decir
    # vendido?"). Primero se habían sacado los renglones de 0 kg —"los que hay
    # 0 no tienen que estar"— y eso rompía dos cosas: la lista dejaba de mostrar
    # justo lo que la competencia premia, y el click de la tabla de Vendidos,
    # que lleva a la fila de esa tela, no encontraba a dónde ir.
    # Lo que molestaba era el CERO pelado, no la fila. Ahora va tachada y con
    # la palabra vendido.
    # ⚠ El que quedó en cero SIN haber vendido nada sí se va: es un ajuste de
    # bodega, no un logro, y no hay nada que ofrecer ahí.
    filas = [f for f in filas
             if float(f.get("stock_kg") or 0) > 0
             or float(f.get("kg_vendidos") or 0) > 0]
    filas.sort(key=lambda f: -float(f.get("puntos_fila") or 0))
    grupos = sorted({f["categoria"] for f in filas if f["categoria"]})
    subgrupos = sorted({(f["subcategoria"], f["categoria"] or "") for f in filas},
                       key=lambda x: x[0])
    return render_template(
        "analisis/parado.html",
        filas=filas, llamados=llamados, mia=True, vend=vend,
        grupos=grupos,
        subgrupos=[{"sub": x, "cat": c} for x, c in subgrupos],
        resumen=queries.resumen(base, queries.kg_al_arrancar()),
        bolsa=queries.bolsa_congelada(),
        grupos_resumen=queries.por_grupo(base),
        estado=queries.estado(),
        # ⭐ La tabla de abajo: qué se vendió, qué día y quién.
        vendidos=queries.vendidos(queries.config("largada", "2026-08-25")),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        ahora_anio=today_ec().year,
    )


def _hoja_saldos() -> dict:
    """Los datos de la lista impresa: una fila por tela × color, con stock.

    ⭐ Dueña 25/08/2026: *"quiero una lista en pdf para imprimir todas las
    telas"*, tela y color.

    Va ordenada por TELA y adentro por kilos, y no por puntos como la pantalla:
    en papel no se filtra ni se busca — se busca con el dedo, y para eso el
    orden tiene que ser alfabético. La pantalla sigue ordenada por puntos, que
    es donde sí se decide a qué ir.
    """
    filas = [f for f in queries.con_puntos(queries.items())
             if float(f["stock_kg"] or 0) > 0]
    filas = queries.abrir_en_lineas(filas)
    filas.sort(key=lambda f: (f["subcategoria"].lower(), -float(f["stock_kg"] or 0)))
    bloques: list[tuple[str, list[dict]]] = []
    for f in filas:
        if not bloques or bloques[-1][0] != f["subcategoria"]:
            bloques.append((f["subcategoria"], []))
        bloques[-1][1].append(f)
    return {
        "filas": filas,
        "bloques": bloques,
        "kg_total": sum(float(f["stock_kg"] or 0) for f in filas),
        "puntos_total": sum(float(f.get("puntos_fila") or 0) for f in filas),
        "estado": queries.estado(),
    }


@analisis_bp.route("/analisis/competencia/telas/imprimir")
@requiere_login
def saldos_imprimir():
    """La lista entera, lista para imprimir. La misma hoja que sale en PDF.

    ⚠ Cuelga de /analisis/competencia a propósito: así la ven también los
    vendedores, que tienen ese prefijo abierto. No lleva un solo cliente
    adentro — son telas, colores y kilos de la fábrica.
    """
    return render_template("analisis/parado_impreso.html", **_hoja_saldos())


@analisis_bp.route("/analisis/competencia/telas/imprimir.pdf")
@requiere_login
def saldos_imprimir_pdf():
    """La misma hoja, en PDF, para mandarla o guardarla.

    Sale del MISMO HTML que la pantalla de arriba (ver `pdf_motor`): no hay una
    segunda plantilla que se despegue a la primera corrección.
    """
    html = render_template("analisis/parado_impreso.html", **_hoja_saldos())
    try:
        blob = pdf_motor.desde_html(html)
    except pdf_motor.SinMotor as e:
        current_app.logger.error("PDF de saldos: %s", e)
        return Response(
            "No se puede generar el PDF: el servidor no tiene un navegador "
            "instalado para imprimirlo. La pantalla de impresión sigue "
            "funcionando normalmente.",
            status=503, mimetype="text/plain; charset=utf-8")
    nombre = f"Saldos {today_ec().strftime('%d-%m-%Y')}.pdf"
    return Response(blob, mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{nombre}"',
        "Cache-Control": "no-store"})


@analisis_bp.route("/analisis/competencia/mi-hoja")
@requiere_login
def mi_hoja():
    """
    La hoja del vendedor: sus clientes y las telas paradas que puede ofrecerles.

    ⭐ Es LA MISMA plantilla que usa la oficina en /analisis/parado/clientes,
    con los datos recortados a su cartera. Dos plantillas para el mismo papel
    divergen a la primera corrección que se le hace a una sola — ya pasó en
    /mi-cartera, donde la oficina y el portal imprimieron órdenes distintos ocho
    días sin que nadie lo notara.

    ⚠ Cuelga de /analisis/competencia/ para que entre en el mismo permiso del
    allowlist el día que se les habilite. La ruta general de la hoja
    (/analisis/parado/clientes) NO se abre nunca: tiene los clientes de todos.
    """
    vend = _vend_actual()
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return render_template(
        "analisis/parado_clientes.html",
        vend=vend, mia=True, ordenes=queries.ORDENES,
        estado=queries.estado(),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        **queries.por_cliente(None, orden, cartera_de=vend))


def _vend_actual() -> str | None:
    """
    El código de vendedor del usuario logueado.

    ⭐ Nunca del querystring: si viniera de la URL, cualquiera vería la cartera
    de cualquiera cambiando tres letras. La ÚNICA excepción es un usuario
    wildcard (la dueña, Andrés), que puede pasar `?vend=` para previsualizar lo
    que ve un vendedor sin pedirle la contraseña a nadie — mismo mecanismo que
    /mi-cartera. Si el que lo manda ES vendedor, gana el suyo.
    """
    propio = (g.user or {}).get("vend")
    if propio:
        return propio
    if "*" in (g.get("permisos") or set()):
        return (request.args.get("vend") or "").strip().upper() or None
    return None


def _numero(texto: str | None, malos: list[str], donde: str) -> float | None:
    """Un % escrito a mano. Devuelve None si está vacío o no se entiende, y en
    el segundo caso lo anota en `malos` para que la pantalla lo diga."""
    t = (texto or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        malos.append(donde)
        return None


# ⚠ Acá vivía `/analisis/metas`, la pantalla para pisar a mano la meta de un
# grupo. Se borró el 25/08/2026 (dueña: "borrar página de metas, no sirve para
# nada"): desde el 24/08 la competencia NO TIENE METAS —gana el que más puntos
# hace— así que la pantalla editaba un número que ya no decide nada. La tabla
# `scintela.parado_meta` queda en la base sin nadie que la lea ni la escriba;
# se borra con la limpieza de septiembre.


@analisis_bp.route("/analisis/parado/actualizar", methods=["POST"])
@requiere_login
@requiere_permiso("analisis.ver")
def parado_actualizar():
    """Trae todo de Asinfo. Tarda ~10 s: por eso es un botón y no pasa al abrir."""
    try:
        r = queries.actualizar()
        flash(f"Actualizado: {r['items']} telas en la lista, "
              f"{r['llamados']} candidatos.", "success")
    except Exception as e:                       # noqa: BLE001
        # Fail-closed: si Asinfo no contesta, la pantalla sigue mostrando la
        # foto vieja CON su fecha, que es información. Vaciarla no lo es.
        _LOG.warning("Análisis/parado: no se pudo actualizar: %s", e)
        flash(f"No se pudo actualizar: {e}. Se sigue viendo la foto anterior.",
              "error")
    return redirect(url_for("analisis.parado"))
