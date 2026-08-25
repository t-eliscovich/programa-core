"""El estado de cuenta como IMAGEN, para mandarlo como foto por WhatsApp.

TMT 2026-08-25, por WhatsApp con Alex Velastegui, después de tres arreglos del
botón verde que no alcanzaron:

    Alex   — *"desde el pdf q genera no permite enviar por wsp"*
    Tamara — *"creo que foto y compartir como imagen si no?"*
    Alex   — *"es una opción sólo q la imagen es muy pequeña"*
    Tamara — *"agrando la imagen"*

Lo que protegen estos tests:
  1. Que la imagen sea LA MISMA hoja que el papel y el PDF — una sola
     plantilla, no una tercera.
  2. Que no le llegue al cliente con el menú del programa adentro.
  3. Que una cartera larga no salga CORTADA por abajo — el error que nadie ve
     hasta que el cliente reclama.
  4. Que el vendedor no pueda pedir la imagen de un cliente ajeno.
  5. Que sin navegador el servidor lo diga en vez de reventar.
"""
from __future__ import annotations

import io
from datetime import date

import bcrypt
import pytest
from PIL import Image

from modules._lib import imagen_motor, pdf_motor
from modules.informes import estado_cuenta_imagen, estado_cuenta_pdf
from modules.mi_cartera import queries as q
from tests.test_estado_cuenta_pdf import _datos, oficina, vendedor  # noqa: F401

# ---------------------------------------------------------------------------
# El alto de la ventana — la trampa propia de sacar una foto
# ---------------------------------------------------------------------------


def test_la_ventana_crece_con_las_filas():
    """`--screenshot` captura EXACTAMENTE el alto de la ventana: lo que no
    entra no sale en la foto Y NO AVISA. Por eso el alto sale de cuántas filas
    tiene la hoja, y se pide de más."""
    assert imagen_motor.alto_para(60) > imagen_motor.alto_para(5)
    # De más, no de menos: las filas reales miden ~38 px.
    assert imagen_motor.alto_para(60) - imagen_motor.alto_para(0) >= 60 * 38


def test_la_ventana_tiene_piso_y_techo():
    """El piso, para que un cliente de una sola factura no salga en una tira.
    El techo, porque el bitmap se paga en memoria (ancho × alto × 4 bytes) y el
    servidor es el mismo que atiende la app."""
    assert imagen_motor.alto_para(0) == imagen_motor._ALTO_MIN
    assert imagen_motor.alto_para(-3) == imagen_motor._ALTO_MIN
    assert imagen_motor.alto_para(99999) == imagen_motor._ALTO_MAX


def _png(alto, alto_dibujado):
    """Un PNG blanco con una franja negra arriba, para probar el recorte."""
    im = Image.new("RGB", (100, alto), (255, 255, 255))
    for y in range(10, alto_dibujado):
        for x in range(10, 90):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_el_recorte_saca_el_blanco_que_sobra():
    """La ventana se pide de más a propósito, así que abajo queda un campo
    blanco. En una foto de WhatsApp ese blanco se lleva lugar de la miniatura,
    que es lo único que el cliente ve antes de decidir si la abre."""
    recortada, cortada = imagen_motor._recortar(_png(900, 200))
    with Image.open(io.BytesIO(recortada)) as im:
        assert im.height < 300, "no recortó el blanco de abajo"
        assert im.width == 100, "el ancho NO se toca: movería las columnas"
    assert cortada is False


def test_el_recorte_avisa_cuando_la_hoja_llego_hasta_el_borde():
    """⭐ La alarma. Si lo dibujado toca el último píxel de la ventana, es casi
    seguro que abajo había MÁS y la ventana lo cortó. Un estado de cuenta al
    que le faltan las últimas facturas se manda sin que nadie lo note — ya pasó
    con el PDF el 04/08."""
    _, cortada = imagen_motor._recortar(_png(300, 300))
    assert cortada is True


def test_una_hoja_en_blanco_no_rompe_el_recorte():
    """Sin nada dibujado no hay caja que recortar. Se devuelve tal cual en vez
    de reventar con un `None`."""
    crudo = _png(200, 0)
    recortada, cortada = imagen_motor._recortar(crudo)
    assert recortada == crudo
    assert cortada is False


def test_si_salio_cortada_se_rehace_UNA_vez_mas_alta(monkeypatch):
    """Rehacerla cuesta un navegador más; mandarle al cliente un estado de
    cuenta al que le faltan facturas cuesta la confianza en el número."""
    altos = []

    def _falso(exe, html, static, alto):
        altos.append(alto)
        # La primera sale tocando el borde (cortada); la segunda, no.
        return _png(alto, alto if len(altos) == 1 else 50)

    monkeypatch.setattr(imagen_motor.pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr(imagen_motor, "_sacar_foto", _falso)
    imagen_motor.desde_html("<html></html>", filas=3)

    assert len(altos) == 2, "no la rehizo"
    assert altos[1] == altos[0] * 2


def test_una_hoja_que_entra_se_saca_UNA_sola_vez(monkeypatch):
    """El camino normal no paga el segundo navegador."""
    veces = []

    def _falso(exe, html, static, alto):
        veces.append(alto)
        return _png(alto, 60)

    monkeypatch.setattr(imagen_motor.pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr(imagen_motor, "_sacar_foto", _falso)
    imagen_motor.desde_html("<html></html>", filas=3)
    assert len(veces) == 1


def test_sin_navegador_lo_dice_en_vez_de_reventar(monkeypatch):
    monkeypatch.setattr(imagen_motor.pdf_motor, "binario", lambda: None)
    assert imagen_motor.disponible() is False
    with pytest.raises(pdf_motor.SinMotor):
        imagen_motor.desde_html("<html></html>")


# ---------------------------------------------------------------------------
# La hoja: una sola plantilla, y sin el menú del programa adentro
# ---------------------------------------------------------------------------


def _html_de(cliente, monkeypatch, *, imagen=True):
    """El HTML que se le manda al navegador, pedido por la RUTA de verdad.

    ⚠ Tiene que pasar por un usuario logueado: `base.html` no dibuja el
    contenido sin sesión, así que renderizar a mano desde un
    `test_request_context` devuelve la página vacía —y el test pasaría
    mirando la nada. (Al PDF le pasa lo mismo; se descubrió acá.)
    """
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))
    visto = {}
    modulo = estado_cuenta_imagen.imagen_motor if imagen else pdf_motor
    monkeypatch.setattr(modulo, "desde_html",
                        lambda html, **kw: visto.setdefault("h", html) and b"x")
    cliente.get("/informes/estado-cuenta/MWI/" + ("imagen" if imagen else "pdf"))
    return visto["h"]


def test_la_imagen_sale_del_MISMO_template_que_el_papel_y_el_pdf(
        oficina, monkeypatch):  # noqa: F811
    """⭐ No hay un estado de cuenta "para imagen". Dos plantillas divergen a la
    primera corrección que se le hace a una sola, y ésta es la hoja que se le
    manda al cliente — el mismo argumento por el que el PDF tampoco tiene la
    suya."""
    html = _html_de(oficina, monkeypatch)
    # Las marcas del template compartido: el bloque por cliente del lote y el
    # cuerpo que incluye (`_estado_cuenta_impreso.html`).
    assert 'class="cli-block"' in html
    assert "MARIO W INNOVANOVENTA" in html
    assert "167080" in html, "no trae la factura: se renderizó una hoja vacía"


def test_las_filas_salen_de_los_datos_y_no_de_medir_el_html(app, monkeypatch):
    """`imagen_motor` necesita el alto ANTES de dibujar, porque la foto captura
    exactamente la ventana y lo que no entra se pierde sin avisar."""
    visto = {}
    monkeypatch.setattr(estado_cuenta_imagen.imagen_motor, "desde_html",
                        lambda html, filas=0, **kw: visto.setdefault("f", filas))
    with app.test_request_context():
        estado_cuenta_imagen.generar(_datos())
    assert visto["f"] == 1


def test_la_foto_no_le_llega_al_cliente_con_el_menu_del_programa(
        oficina, monkeypatch):  # noqa: F811
    """⚠ El PDF sale con `--print-to-pdf`, que renderiza con `@media print`, y
    ahí `base.html` ya esconde solo el sidebar, el encabezado y los botones. La
    imagen sale con `--screenshot`, que renderiza con `@media screen`: nada de
    eso se esconde. Sin el flag, la foto que le llega al cliente vendría con el
    menú del programa adentro."""
    html = _html_de(oficina, monkeypatch)
    # La marca del bloque `{% if imagen %}`, que es lo único que lo distingue
    # de la hoja impresa.
    bloque = html.split("main, .max-w-6xl { max-width: none !important;")
    assert len(bloque) == 2, "no salió el bloque de la imagen"
    # ⚠ El chrome se busca DENTRO del bloque de la imagen: `base.html` trae los
    # mismos selectores en su `@media print`, así que buscarlos en la página
    # entera daría verde aunque el flag no hiciera nada.
    css = bloque[0].rsplit("{% endif %}", 1)[-1].rsplit("<style>", 1)[-1]
    assert ".no-print, aside, header, nav," in css, "no esconde el chrome en pantalla"
    # Las flechitas ↕ de ordenar: ofrecen un gesto que en una imagen no existe.
    assert ".sort-icon { display: none !important; }" in css


def test_el_pdf_NO_lleva_el_bloque_de_la_imagen(oficina, monkeypatch):  # noqa: F811
    """El otro lado del flag: en papel el chrome ya lo esconde `@media print`,
    y pisar los anchos ahí rompería la hoja impresa, que es la que se usa todos
    los días."""
    html = _html_de(oficina, monkeypatch, imagen=False)
    assert "main, .max-w-6xl { max-width: none !important;" not in html
    assert 'class="cli-block"' in html, "y sigue siendo la misma hoja"


def test_el_archivo_se_llama_igual_que_el_pdf_pero_png():
    """⭐ Mismo criterio —código y día, sin el nombre largo— por el mismo motivo
    que el PDF: estos archivos no se abren de a uno. Se delega en vez de
    copiarse, así que si mañana cambia el criterio, cambia para los dos."""
    png = estado_cuenta_imagen.nombre_archivo("MARIO W INNOVANOVENTA S.A", "MWI")
    pdf = estado_cuenta_pdf.nombre_archivo("MARIO W INNOVANOVENTA S.A", "MWI")
    assert png.endswith(".png")
    assert png[:-4] == pdf[:-4], "los dos nombres se separaron"
    assert "MWI" in png
    assert "MARIO" not in png, "el nombre largo no va: WhatsApp lo corta"


def test_las_filas_cuentan_facturas_Y_cheques():
    """Los dos bloques ocupan alto. Contar sólo las facturas deja la ventana
    corta justo en los clientes que tienen cheques en cartera."""
    data = _datos()
    data["cheques"] = [{"numero": 1}, {"numero": 2}]
    assert estado_cuenta_imagen.cuantas_filas(data) == 3
    assert estado_cuenta_imagen.cuantas_filas({}) == 0


# ---------------------------------------------------------------------------
# Las dos puertas: la oficina y el vendedor
# ---------------------------------------------------------------------------


def test_la_oficina_baja_la_imagen(oficina, monkeypatch):  # noqa: F811
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))
    monkeypatch.setattr(estado_cuenta_imagen.imagen_motor, "desde_html",
                        lambda html, **kw: b"\x89PNG-falso")

    r = oficina.get("/informes/estado-cuenta/MWI/imagen")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    # ⭐ `inline` y NO `attachment`: el camino bueno es que el vendedor VEA la
    # foto y la mantenga apretada. Con `attachment` el teléfono la baja a una
    # carpeta y volvemos al problema del PDF — un archivo que hay que ir a
    # buscar, que es exactamente lo que Alex no pudo hacer.
    cd = r.headers["Content-Disposition"]
    assert cd.startswith("inline;")
    assert "MWI" in cd and cd.endswith('.png"')


def test_un_cliente_que_no_existe_da_404(oficina, monkeypatch):  # noqa: F811
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: {})
    assert oficina.get("/informes/estado-cuenta/ZZZ/imagen").status_code == 404


def test_el_vendedor_no_puede_pedir_la_imagen_de_un_cliente_ajeno(
        vendedor, monkeypatch):  # noqa: F811
    """El mismo guard que el PDF y que la ficha. Una puerta nueva que no lo
    tuviera sería una forma de ver la cartera de otro."""
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: False)
    assert vendedor.get("/mi-cartera/cliente/AJE/imagen").status_code == 404


def test_sin_navegador_la_imagen_explica_en_vez_de_dar_500(
        oficina, monkeypatch):  # noqa: F811
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))

    def _explota(html, **kw):
        raise pdf_motor.SinMotor("El navegador tardó demasiado en sacar la imagen.")

    monkeypatch.setattr(estado_cuenta_imagen.imagen_motor, "desde_html", _explota)
    r = oficina.get("/informes/estado-cuenta/MWI/imagen")
    assert r.status_code == 503
    # Y dice el motivo REAL, no siempre "falta instalar un navegador".
    assert "tardó demasiado" in r.data.decode()


def test_la_fecha_del_nombre_es_la_de_hoy():
    """Distingue el que se mandó hoy del de la semana pasada, que es la
    pregunta que aparece cuando el cliente discute un saldo."""
    from filters import today_ec

    assert today_ec().strftime("%d-%m-%Y") in estado_cuenta_imagen.nombre_archivo(
        "X", "MWI")
    assert "/" not in estado_cuenta_imagen.nombre_archivo("X", "MWI"), (
        "la barra es separador de carpetas y rompe el archivo")


def test_el_estado_de_cuenta_de_la_imagen_es_de_una_fecha_real():
    """Guarda de humo: que `_datos` siga trayendo una factura con fecha, que es
    lo que hace que la hoja tenga filas que contar."""
    assert _datos()["facturas"][0]["fecha"] == date(2026, 1, 15)


# ---------------------------------------------------------------------------
# La hoja de UNA factura, también como foto — TMT 2026-08-25: "si dale"
# ---------------------------------------------------------------------------


def _factura(vendedor, monkeypatch, formato, det=None):
    """Pide la hoja de una factura del vendedor en el formato pedido."""
    from modules.mi_cartera import views
    from tests.test_mi_cartera import _ec_con_facturas

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    import modules.asinfo.factura_lineas as fl
    monkeypatch.setattr(fl, "que_se_llevo", lambda n: det if det is not None
                        else {"estado": "sin-datos", "lineas": [], "servicios": [],
                              "totales": {}})
    return vendedor.get(f"/mi-cartera/cliente/TDV/factura/100/{formato}")


def test_la_factura_sale_como_png(vendedor, monkeypatch):  # noqa: F811
    monkeypatch.setattr(imagen_motor, "desde_html", lambda h, **kw: b"\x89PNG")
    r = _factura(vendedor, monkeypatch, "imagen")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    cd = r.headers["Content-Disposition"]
    assert cd.startswith("inline;") and cd.endswith('.png"')
    assert "Factura" in cd and "TDV" in cd


def test_el_pdf_de_la_factura_sigue_saliendo_igual(vendedor, monkeypatch):  # noqa: F811
    """El formato viejo no se toca: comparten cuerpo, no destino."""
    monkeypatch.setattr(pdf_motor, "desde_html", lambda h, **kw: b"%PDF-1.4")
    r = _factura(vendedor, monkeypatch, "pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.headers["Content-Disposition"].endswith('.pdf"')


def test_las_filas_de_la_factura_cuentan_lineas_Y_servicios(
        vendedor, monkeypatch):  # noqa: F811
    """El alto de la foto sale de los renglones de la hoja. Contar sólo las
    líneas deja la ventana corta en las facturas que llevan servicios."""
    visto = {}
    monkeypatch.setattr(imagen_motor, "desde_html",
                        lambda h, filas=0, **kw: visto.setdefault("f", filas) and b"x")
    det = {"estado": "ok", "lineas": [{}, {}, {}], "servicios": [{}],
           "totales": {"rollos": 3, "kg": 10}}
    _factura(vendedor, monkeypatch, "imagen", det=det)
    assert visto["f"] == 4


def test_sin_navegador_la_factura_dice_el_motivo_REAL(vendedor, monkeypatch):  # noqa: F811
    """🐞 El 503 de esta ruta decía SIEMPRE *"el servidor no tiene un navegador
    instalado"*, igual que el del estado de cuenta antes del 25/08. `SinMotor`
    se levanta por tres motivos y los otros dos son los que le pasan al
    vendedor con una factura grande."""
    def _explota(h, **kw):
        raise pdf_motor.SinMotor("El navegador tardó demasiado en sacar la imagen.")

    monkeypatch.setattr(imagen_motor, "desde_html", _explota)
    r = _factura(vendedor, monkeypatch, "imagen")
    assert r.status_code == 503
    texto = r.data.decode()
    assert "tardó demasiado" in texto
    assert "no tiene un navegador instalado" not in texto
    assert "la imagen" in texto, "dice PDF cuando lo que falló fue la foto"


# ---------------------------------------------------------------------------
# El ancho y la fecha — TMT 2026-08-25, mirando la foto al lado del papel
# ---------------------------------------------------------------------------


def test_la_foto_no_se_saca_mas_ancha_que_la_hoja_de_papel():
    """⚠ TMT 2026-08-25: *"¿los anchos del pdf son así?"*.

    La tabla del estado de cuenta NO tiene anchos fijos: es elástica y el
    navegador reparte el sobrante entre las columnas. Cuanto más ancha la hoja,
    más se abren los huecos — a 1100 px se notaba entre Número e Importe.

    El papel sale a 794 px (A4). La foto se saca cerca de eso para que se
    parezca, y bajarlo no cuesta legibilidad: la letra mide lo mismo en
    píxeles, cambia sólo el aire al costado.
    """
    assert imagen_motor.ANCHO <= 950, "la foto se volvió a estirar"
    assert imagen_motor.ANCHO >= 800, "más angosta que A4: las columnas no entran"


def test_la_foto_lleva_el_DIA_adentro(oficina, monkeypatch):  # noqa: F811
    """⭐ TMT 2026-08-25: *"¿el nombre es el mismo?"*.

    El archivo se llama igual que el PDF —código y día, decisión del 24/08
    porque *"el nombre es lo primero que ve quien lo recibe en el chat"*—. Pero
    WhatsApp muestra el nombre de un DOCUMENTO y no el de una FOTO: al mandar
    la imagen, el día deja de verse.

    Así que el día va ADENTRO de la foto, donde se lee sin abrir nada. El
    cliente y el código ya estaban en el encabezado; faltaba la fecha, que es
    la que distingue el estado que se mandó hoy del de la semana pasada.
    """
    from filters import fecha_es, today_ec

    # ⚠ Por la RUTA, no a mano: sin usuario logueado `base.html` no dibuja el
    # contenido y el test pasaría mirando una página vacía (ver `_html_de`).
    html = _html_de(oficina, monkeypatch)
    assert 'class="ph-fecha"' in html, "la foto no dice de qué día es"
    assert fecha_es(today_ec()) in html


def test_el_papel_NO_lleva_esa_fecha(oficina, monkeypatch):  # noqa: F811
    """El otro lado: la hoja impresa es la que la oficina usa todos los días y
    no se le agrega nada. La fecha es de la FOTO, que es la que pierde el
    nombre del archivo al mandarse."""
    html = _html_de(oficina, monkeypatch, imagen=False)
    assert 'class="ph-fecha"' not in html
