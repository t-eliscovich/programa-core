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

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso, tiene_permiso
from filters import today_ec
from modules.informes import queries as informes_queries
from modules.informes import views as informes_views
from parsers import parse_int
from scope_vendedor import vendedor_de

from . import portal_cliente, queries

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
    """Lo que necesita el cascarón (nombre, código, link de preview).

    ⭐ El redondel de la cuenta lleva el CÓDIGO del vendedor —tres letras—, no
    dos iniciales de su nombre. TMT 2026-08-11: *"todos lados donde aparece el
    código, mantené nuestros códigos que tienen 3 letras, no de a dos"*. Las
    iniciales eran decoración; el código es el handle con el que la fábrica lo
    nombra. Se borró la variable `iniciales` en vez de rellenarla con el
    código: dejar el nombre viejo apuntando a otro dato es exactamente cómo se
    lee la equivocada dentro de seis meses. El template usa `vend`, que ya
    estaba acá.
    """
    return {
        "vend": (vend or "").upper(),
        "vend_nombre": queries.nombre_vendedor(vend),
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
        # A dónde lleva el número de factura de la tabla (ver _movimientos).
        factura_endpoint="mi_cartera.factura",
        factura_args={"codigo_cli": codigo_cli},
        tab=(request.args.get("tab") or "facturas").strip().lower(),
        # El acceso de ESTE cliente al portal. Va acá, donde el vendedor ya
        # está parado, y no en una pantalla nueva que nadie abre.
        portal=portal_cliente.estado(codigo_cli),
        **data,
        **_ctx_base(vend),
    )


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/portal", methods=["POST"])
@requiere_login
@requiere_permiso("micartera.ver")
def portal_acceso(codigo_cli: str):
    """Cortarle o reabrirle al cliente el acceso al portal.

    ⭐ Es el control de todo el diseño del portal: se entra con el código y el
    RUC, que son públicos, y lo que frena al que no debería estar es que el
    vendedor lo ve y le corta. Por eso vive donde el vendedor trabaja.

    Pasa por `_cargar_cliente` ANTES de tocar nada: ese guard es el que
    verifica que el cliente sea SUYO. Sin él, tipear el código de un cliente
    ajeno sería cortarle el acceso a un cliente de otro vendedor.
    """
    vend = _vend_actual()
    _cargar_cliente(vend, codigo_cli)      # 404 si no es suyo

    quien = (getattr(g, "user", None) or {}).get("username") or vend
    accion = (request.form.get("accion") or "").strip()
    if accion == "reabrir":
        ok, msg = portal_cliente.reabrir(codigo_cli, quien)
    else:
        ok, msg = portal_cliente.cortar(codigo_cli, quien)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("mi_cartera.cliente", codigo_cli=codigo_cli))


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


def _factura_de(data: dict, numf: int) -> dict:
    """La factura `numf` DENTRO del estado de cuenta de este cliente.

    El scope no es un chequeo aparte: si la factura no está en la cuenta del
    cliente que `_cargar_cliente` ya autorizó, no existe para esta pantalla.
    Tipear el número de una factura ajena en la barra de direcciones da 404
    porque el número se busca en una lista que ya viene acotada.
    """
    facturas = (data.get("facturas") or []) + (data.get("facturas_totalizadas") or [])
    for f in facturas:
        try:
            if int(f.get("numf") or 0) == int(numf):
                return f
        except (TypeError, ValueError):
            continue
    abort(404)
    return {}  # pragma: no cover - abort() corta


def _factura_ctx(codigo_cli: str, numf: int):
    """(vend, cliente, factura, detalle, número) — o 404 por el camino."""
    from modules.asinfo import factura_lineas

    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    f = _factura_de(data, numf)
    numero = (f.get("numf_completo") or "").split("-")[-1].lstrip("0") or str(numf)
    return vend, data.get("cliente") or {}, f, factura_lineas.que_se_llevo(
        f.get("numf_completo")), numero


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/factura/<int:numf>")
@requiere_login
@requiere_permiso("micartera.ver")
def factura(codigo_cli: str, numf: int):
    """Qué se llevó el cliente en esta factura.

    TMT 2026-08-25 (dueña): *"hacerlo también para vendedores y que puedan
    compartir"*. El vendedor está parado en el local y lo que le preguntan es
    qué mandaron en esa factura; hasta hoy eso sólo estaba en el papel.
    """
    vend, cliente, f, det, numero = _factura_ctx(codigo_cli, numf)
    return render_template("mi_cartera/factura.html", cliente=cliente, f=f,
                           det=det, numero=numero, seccion="clientes",
                           **_ctx_base(vend))


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/factura/<int:numf>/hoja")
@requiere_login
@requiere_permiso("micartera.ver")
def factura_hoja(codigo_cli: str, numf: int):
    """La hoja para imprimir. Es la MISMA que se manda por WhatsApp."""
    _v, cliente, f, det, numero = _factura_ctx(codigo_cli, numf)
    return render_template("mi_cartera/factura_hoja.html", cliente=cliente,
                           f=f, det=det, numero=numero)


def _factura_archivo(codigo_cli: str, numf: int, formato: str):
    """La hoja de UNA factura como archivo: PDF o imagen, según `formato`.

    Las dos rutas de abajo son la misma respuesta con otro formato —mismo
    guard, misma hoja, mismo nombre de archivo, mismo 503— así que comparten
    cuerpo en vez de copiarse. Es la misma decisión que `responder_pdf` y
    `responder_imagen` en `informes`.

    🐞 TMT 2026-08-25: el 503 decía SIEMPRE *"el servidor no tiene un navegador
    instalado"*, y `SinMotor` se levanta por tres motivos —no hay navegador, el
    navegador tardó más que `TIMEOUT_S`, o no devolvió nada—. Los dos últimos
    son los que le pasan al vendedor con una factura grande, y contestarle que
    falta instalar algo manda a buscar el problema al lugar equivocado. Ahora
    cada motivo dice lo suyo. (El mismo arreglo que en `informes.views`.)
    """
    import re

    from flask import Response, current_app

    from modules._lib import imagen_motor, pdf_motor

    _v, cliente, f, det, numero = _factura_ctx(codigo_cli, numf)
    html = render_template("mi_cartera/factura_hoja.html", cliente=cliente,
                           f=f, det=det, numero=numero)
    es_imagen = formato == "imagen"
    try:
        if es_imagen:
            # `factura_hoja.html` es una página suelta —no extiende
            # `base.html`— así que no hay chrome del programa que esconder: no
            # necesita el `imagen=True` que sí necesita el estado de cuenta.
            filas = len(det.get("lineas") or []) + len(det.get("servicios") or [])
            blob = imagen_motor.desde_html(html, filas=filas)
        else:
            blob = pdf_motor.desde_html(html)
    except pdf_motor.SinMotor as e:
        current_app.logger.error("Factura %s (%s): %s", numero, formato, e)
        return Response(
            f"No se pudo generar {'la imagen' if es_imagen else 'el PDF'}. {e} "
            "La pantalla de impresión sigue funcionando normalmente.",
            status=503, mimetype="text/plain; charset=utf-8",
        )
    # Mismo criterio que el estado de cuenta: código y fecha, sin tildes ni
    # barras. Quien lo recibe junta varios en el chat y los ordena solos.
    cod = re.sub(r"[^A-Za-z0-9]", "", (codigo_cli or "")).upper() or "CLIENTE"
    ext = "png" if es_imagen else "pdf"
    nombre = f"Factura {numero} {cod} {today_ec().strftime('%d-%m-%Y')}.{ext}"
    return Response(blob, mimetype="image/png" if es_imagen else "application/pdf",
                    headers={
                        "Content-Disposition": f'inline; filename="{nombre}"',
                        "Cache-Control": "no-store",
                    })


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/factura/<int:numf>/pdf")
@requiere_login
@requiere_permiso("micartera.ver")
def factura_pdf(codigo_cli: str, numf: int):
    """La misma hoja, como archivo, para mandársela al cliente.

    El 503 no es decorativo: el botón se esconde cuando no hay motor, pero
    alguien puede llegar por la URL, y un mensaje que dice qué falta es lo que
    evita el reporte de "no anda el botón".
    """
    return _factura_archivo(codigo_cli, numf, "pdf")


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/factura/<int:numf>/imagen")
@requiere_login
@requiere_permiso("micartera.ver")
def factura_imagen(codigo_cli: str, numf: int):
    """La misma hoja, como FOTO.

    TMT 2026-08-25: *"si dale"*, después de ver que esta pantalla tenía el
    mismo problema que el estado de cuenta. Es el mismo caso de Alex —el
    teléfono que no comparte documentos— sobre la otra hoja que el vendedor le
    manda al cliente. Una foto se manda con el gesto que ya usa todos los días.
    """
    return _factura_archivo(codigo_cli, numf, "imagen")


@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/imagen")
@requiere_login
@requiere_permiso("micartera.ver")
def imagen(codigo_cli: str):
    """El estado de cuenta como IMAGEN, para mandarlo como foto por WhatsApp.

    TMT 2026-08-25, con Alex Velastegui: *"desde el pdf q genera no permite
    enviar por wsp"* → Tamara: *"creo que foto y compartir como imagen si
    no?"*.

    En un teléfono, mandar una FOTO lo sabe hacer cualquiera y lo permite
    cualquier aparato; mandar un DOCUMENTO, no. Por eso el vendedor llegaba al
    PDF y se quedaba ahí. Ver `imagen_motor` para el razonamiento entero.

    Es LA MISMA hoja que el PDF y que el papel, sacada como foto — misma
    plantilla, mismos números. Y el mismo guard que las otras dos rutas:
    `_cargar_cliente` 404ea si el cliente no es de este vendedor.
    """
    vend = _vend_actual()
    data = _cargar_cliente(vend, codigo_cli)
    return informes_views.responder_imagen(data, (codigo_cli or "").upper())


@mi_cartera_bp.route("/mi-cartera/prueba-envio")
@requiere_login
@requiere_permiso("micartera.ver")
def prueba_envio():
    """Pantalla de prueba del envío por WhatsApp, para abrir en el teléfono.

    TMT 2026-08-25, después de dos arreglos: *"sigue sin funcionar"* → *"no
    abre el otro programa, aunque esté generado"*. El PDF se prepara bien y el
    segundo toque no abre WhatsApp — en UN teléfono y no en los otros.

    Adivinar desde acá ya se agotó: el vendedor abre esta pantalla en SU
    aparato, toca los dos botones y manda una foto. La pantalla dice qué
    teléfono es, qué puede y qué no, y el ERROR EXACTO del compartir.

    Usa el PRIMER cliente del vendedor sólo para tener un PDF de verdad que
    pedir; no muestra un solo dato del cliente.
    """
    vend = _vend_actual()
    mios = sorted(queries.mis_clientes(vend),
                  key=lambda c: (c.get("codigo_cli") or "").upper())
    if not mios:
        abort(404)
    cod = mios[0]["codigo_cli"]
    pdf_url = url_for("mi_cartera.pdf", codigo_cli=cod)
    if request.args.get("vend"):
        pdf_url += "?vend=" + (request.args.get("vend") or "").strip().upper()
    return render_template(
        "mi_cartera/prueba_envio.html",
        pdf_url=pdf_url,
        seccion="",
        **_ctx_base(vend),
    )


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
