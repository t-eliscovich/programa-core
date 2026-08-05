"""El estado de cuenta como PDF, para mandárselo al cliente por WhatsApp.

TMT 2026-08-04 (dueña): primero lo pidió para el vendedor en la calle y a los
dos minutos lo amplió — *"dejá esto de enviar por WhatsApp para todos los
usuarios, no sólo vendedores; quizás Alex le puede mandar al cliente
también"*.

Lo que protegen estos tests:
  1. Que el PDF sea LA MISMA hoja que se imprime, no una segunda plantilla.
  2. Que el vendedor no pueda pedir el PDF de un cliente ajeno.
  3. Que sin motor de PDF el servidor lo diga en vez de reventar.
  4. Que el teléfono que se le pasa a WhatsApp esté bien formado — un número
     mal armado abre un chat con un desconocido.
"""
from __future__ import annotations

from datetime import date

import bcrypt
import pytest

from filters import wa_tel
from modules._lib import pdf_motor
from modules.informes import estado_cuenta_pdf
from modules.mi_cartera import queries as q
from tests.test_mi_cartera import _totales

# ---------------------------------------------------------------------------
# El teléfono para el link de WhatsApp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # Como está cargado en el dBase: prefijo nacional 0.
        ("0989506447", "593989506447"),
        ("099 123 4567", "593991234567"),
        ("032-745-123", "59332745123"),
        # El 0 se REEMPLAZA por 593, no se le pega adelante. Si esto se
        # rompiera, wa.me abriría un chat con un número que no existe.
        ("0989506447", "593989506447"),
        # Ya internacional: se deja.
        ("+593989506447", "593989506447"),
        ("593989506447", "593989506447"),
        ("00593989506447", "593989506447"),
        # Fichas con VARIOS números: gana el primero. Mandarle el estado de
        # cuenta al fax de la empresa no sirve.
        ("0989506447 / 032745123", "593989506447"),
        ("0989506447, 0987654321", "593989506447"),
        # ⭐ El caso que se rompió en producción el 04/08: LUIS ENRIQUE
        # PILATAXI tiene dos celulares separados por un ESPACIO y el botón
        # le salía sin teléfono. Cortar por espacios tampoco sirve —ver el
        # caso de abajo, un solo número escrito con espacios—, así que se
        # corta por largo, no por separador.
        ("0991271637 0993391393", "593991271637"),
        ("099 123 4567", "593991234567"),
        ("0991271637  0993391393  032745123", "593991271637"),
        # Basura → '' y la pantalla no ofrece el botón.
        ("", ""), (None, ""), ("s/n", ""), ("123", ""), ("-", ""),
        ("0" * 20, ""),
    ],
)
def test_wa_tel(crudo, esperado):
    assert wa_tel(crudo) == esperado


# ---------------------------------------------------------------------------
# El nombre del archivo — es lo primero que ve el cliente en el chat
# ---------------------------------------------------------------------------


def test_el_archivo_se_llama_como_el_cliente():
    assert estado_cuenta_pdf.nombre_archivo("MARIO W INNOVANOVENTA S.A", "MWI") == \
        "Estado de cuenta - MARIO W INNOVANOVENTA S.A.pdf"


def test_el_nombre_del_archivo_aguanta_cualquier_cosa():
    """Pasa por WhatsApp, por mail y por el disco de quien lo reciba."""
    # Tildes y ñ: fuera, no todos los sistemas las bancan en un nombre.
    assert "Nunez" in estado_cuenta_pdf.nombre_archivo("Núñez", "NUN")
    # Barras y dos puntos romperían la ruta al guardarlo.
    assert "/" not in estado_cuenta_pdf.nombre_archivo("A/B: C", "ABC")
    # Sin nombre cae al código: nunca un archivo sin identificar.
    assert estado_cuenta_pdf.nombre_archivo("", "tdv") == "Estado de cuenta - TDV.pdf"
    assert estado_cuenta_pdf.nombre_archivo("!!!", "tdv") == "Estado de cuenta - TDV.pdf"
    # Y siempre termina en .pdf.
    assert estado_cuenta_pdf.nombre_archivo("X" * 200, "TDV").endswith(".pdf")


# ---------------------------------------------------------------------------
# El motor
# ---------------------------------------------------------------------------


def test_sin_navegador_avisa_en_vez_de_reventar(monkeypatch):
    monkeypatch.setattr(pdf_motor, "binario", lambda: None)
    assert pdf_motor.disponible() is False
    with pytest.raises(pdf_motor.SinMotor):
        pdf_motor.desde_html("<html></html>")


def test_el_no_se_reconsulta_pero_el_si_se_cachea(monkeypatch, tmp_path):
    """La misma lección que la columna `vend` (03/08) y que Metabase (29/07):
    un NO no puede vivir tanto como un SÍ. Si mañana instalan Edge en el
    servidor, la app tiene que enterarse sin que nadie la reinicie."""
    pdf_motor._resetear_cache()
    monkeypatch.setattr(pdf_motor.shutil, "which", lambda n: None)
    assert pdf_motor.binario() is None
    # Dentro del TTL no vuelve a mirar el disco en cada request.
    llamadas = []
    monkeypatch.setattr(pdf_motor.shutil, "which",
                        lambda n: llamadas.append(n) or None)
    assert pdf_motor.binario() is None
    assert llamadas == []
    # Pasa el TTL (instalaron el navegador en el medio) → lo encuentra.
    monkeypatch.setattr(pdf_motor, "TTL_NEGATIVO_S", 0.0)
    falso = tmp_path / "chromium"
    falso.write_text("")
    monkeypatch.setattr(pdf_motor.shutil, "which",
                        lambda n: str(falso) if n == "chromium" else None)
    assert pdf_motor.binario() == str(falso)
    # Y una vez que lo tiene, no busca nunca más.
    monkeypatch.setattr(pdf_motor.shutil, "which", lambda n: None)
    assert pdf_motor.binario() == str(falso)
    pdf_motor._resetear_cache()


def test_el_html_queda_listo_para_abrirse_sin_red(tmp_path):
    """Dos cosas que, sin arreglar, dan un PDF en blanco o un cuelgue.

    · `/static/tailwind.css` es una ruta del servidor web y el navegador va a
      abrir un `file://`: sin reescribir, el PDF sale sin una sola regla de
      estilo (texto negro apilado).
    · Los `<script src="https://…">` (htmx viene de unpkg) se sacan: el
      headless corre en el servidor, que puede no tener salida a internet, y
      esperar un script que nunca llega es la forma más común de que el render
      se cuelgue hasta el timeout.
    """
    html = ('<link href="/static/tailwind.css" rel="stylesheet">'
            '<script src="https://unpkg.com/htmx.org@1.9.12"></script>'
            '<script defer src="/static/app.js?v=14"></script>'
            '<p>hola</p>')
    salida = pdf_motor._para_imprimir_offline(html, tmp_path)
    assert "/static/tailwind.css" not in salida
    assert tmp_path.resolve().as_uri() + "/tailwind.css" in salida
    assert "unpkg.com" not in salida
    # El estático local NO se toca: sigue estando, sólo que apuntando al disco.
    assert "app.js" in salida
    assert "<p>hola</p>" in salida


# ---------------------------------------------------------------------------
# Las dos puertas y la misma cocina
# ---------------------------------------------------------------------------


def _datos(cod="MWI"):
    return {
        "cliente": {"codigo_cli": cod, "nombre": "MARIO W INNOVANOVENTA S.A",
                    "provincia": "STO DOMING", "canton": "STO DOMING",
                    "ruc": "2390646219001", "telefono": "0989506447",
                    "vend": "RMY", "pago": "C", "descuento": 14.0,
                    "stop": "", "pase": "", "cupo": 0,
                    "mail": {"mail": "x@y.com", "origen": "", "etiqueta": "",
                             "alternativo": "", "sugerido": "", "origen_alt": "",
                             "incompleto": ""},
                    "direccion1": "MACHALA", "direccion2": ""},
        "facturas": [{"id_factura": 1, "numf": 167080,
                      "numf_completo": "001-099-000167080",
                      "fecha": date(2026, 1, 15), "vencimiento": date(2026, 2, 14),
                      "importe": 7592.26, "abono": 0.0, "saldo": 7592.26,
                      "stat": "A", "tipo": "FA", "kg": 0, "condic": ""}],
        "cheques": [], "anticipos": [],
        "totales": _totales(importe=7592.26, saldo=7592.26, saldo_neto=7592.26,
                            saldo_vivo=7592.26, n_vencidas=1),
    }


@pytest.fixture()
def vendedor(app, client, fake_db):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    fake_db.add_user("pablo", bcrypt.hashpw(b"V2026", bcrypt.gensalt(rounds=4)),
                     rid, vend="PPR")
    client.post("/login", data={"username": "pablo", "password": "V2026"})
    return client


@pytest.fixture()
def oficina(app, fake_db):
    rid = fake_db.add_role("Oficina", ["informes.ver"])
    uid = fake_db.add_user("alex", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_el_pie_de_facturas_tiene_TODAS_las_columnas_de_la_tabla(app):
    """⭐ El pie se corría una columna, pero SÓLO en el papel.

    Dueña 2026-08-04, con el PDF de LEP: *"está bastante desprolijo"*. El
    rótulo "Totales" abarcaba 3 columnas (Fecha, Número, Tipo), pero Tipo y
    Stat son `no-print`: al imprimir esas columnas no quedan vacías, DESAPARECEN,
    y entonces el colspan se comía Importe. En el PDF el total de importes caía
    bajo ABONADO y el de abonos bajo SALDO. Los números estaban bien: era
    alineación, y del lado que no se ve en pantalla.

    Este test cuenta celdas: encabezado y pie tienen que tener las mismas, y
    las que se esconden al imprimir tienen que estar en los dos.
    """
    import re
    from pathlib import Path

    tpl = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text()
    sin_comentarios = re.sub(r"\{#.*?#\}", "", tpl, flags=re.S)
    encabezado = sin_comentarios.split("<thead", 1)[1].split("</thead>", 1)[0]
    pie = sin_comentarios.split("<tfoot", 1)[1].split("</tfoot>", 1)[0]

    n_th = len(re.findall(r"<th\b", encabezado))
    # El pie: celdas sueltas + lo que abarca el colspan.
    n_td = len(re.findall(r"<td\b", pie))
    colspans = [int(c) for c in re.findall(r'colspan="(\d+)"', pie)]
    assert n_td + sum(colspans) - len(colspans) == n_th, (
        f"el pie cubre {n_td + sum(colspans) - len(colspans)} columnas y la "
        f"tabla tiene {n_th}")

    # Y las columnas que no se imprimen tienen celda propia en el pie, para
    # que desaparezcan de las dos filas a la vez.
    assert encabezado.count("no-print") == pie.count("no-print")


def test_el_pie_de_cheques_tambien_cubre_todas_sus_columnas(app):
    """El mismo control que el de facturas, para la otra tabla.

    Al mover Importe delante de Banco (dueña 2026-08-04: *"así se puede ver
    monto abajo de monto"*) el colspan del rótulo tenía que bajar de 6 a 5, o
    el total del mes se iba a imprimir bajo BANCO. Es exactamente el error que
    el pie de facturas ya cometió dos veces: los números bien y la alineación
    mal, que es la peor combinación porque no hay nada que "no cierre".
    """
    import re
    from pathlib import Path

    tpl = re.sub(r"\{#.*?#\}", "", Path(
        "modules/informes/templates/informes/_estado_cuenta_impreso.html"
    ).read_text(), flags=re.S)
    # La segunda tabla del parcial es la de cheques.
    cheques = tpl.split("<thead", 2)[2]
    encabezado = cheques.split("</thead>", 1)[0]
    pie = cheques.split("<tfoot", 1)[1].split("</tfoot>", 1)[0]

    n_th = len(re.findall(r"<th\b", encabezado))
    n_td = len(re.findall(r"<td\b", pie))
    colspans = [int(c) for c in re.findall(r'colspan="(\d+)"', pie)]
    assert n_td + sum(colspans) - len(colspans) == n_th

    # Y el orden que pidió: Importe ANTES que Banco.
    assert encabezado.index(">Importe<") < encabezado.index(">Banco<")


def test_el_pdf_sale_del_template_que_ya_se_imprime(app, monkeypatch):
    """⭐ La invariante de todo esto.

    El primer intento fue armar el PDF en el celular con html2canvas. Andaba,
    y estaba mal: la hoja linda vive en `@media print`, así que para que el
    PDF saliera igual había que copiar esas reglas a otro lado — dos versiones
    de la misma hoja, y esta es la que se le manda al cliente. Ahora el PDF se
    genera imprimiendo el MISMO template. Si alguien le hace uno propio, acá
    se cae.
    """
    visto = {}

    def _render(tpl, **ctx):
        visto["tpl"] = tpl
        visto["n"] = len(ctx.get("clientes") or [])
        return "<html>hoja</html>"

    monkeypatch.setattr(estado_cuenta_pdf, "render_template", _render)
    monkeypatch.setattr(estado_cuenta_pdf.pdf_motor, "desde_html",
                        lambda h: b"%PDF-1.4 " + h.encode())

    with app.test_request_context("/"):
        out = estado_cuenta_pdf.generar(_datos())

    assert visto["tpl"] == "informes/estado_cuenta_lote_print.html"
    assert visto["n"] == 1
    assert out.startswith(b"%PDF")


def test_el_vendedor_no_saca_el_pdf_de_un_cliente_ajeno(vendedor, monkeypatch):
    """La misma fuga que la ficha: alcanzaría con tipear el código en la URL."""
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: False)
    assert vendedor.get("/mi-cartera/cliente/AJENO/pdf").status_code == 404


def test_las_dos_puertas_devuelven_EL_MISMO_archivo(vendedor, oficina, monkeypatch):
    """Dueña 2026-08-04: el vendedor y Alex mandan lo mismo.

    Si divergieran, el cliente podría recibir dos estados de cuenta distintos
    del mismo día según quién se lo mandó.
    """
    from modules.informes import queries as iq
    from modules.mi_cartera import views as mv

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(mv.informes_queries, "estado_cuenta_cliente",
                        lambda cod: _datos(cod))
    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))
    monkeypatch.setattr(pdf_motor, "desde_html", lambda h: b"%PDF-1.4 igual")

    a = vendedor.get("/mi-cartera/cliente/MWI/pdf")
    b = oficina.get("/informes/estado-cuenta/MWI/pdf")
    assert a.status_code == b.status_code == 200
    assert a.data == b.data
    assert a.mimetype == b.mimetype == "application/pdf"
    # Y el archivo llega con el nombre del cliente, que es lo que el cliente
    # ve en el chat antes de abrirlo.
    for r in (a, b):
        assert "MARIO W INNOVANOVENTA S.A.pdf" in r.headers["Content-Disposition"]


def test_sin_motor_el_servidor_explica_en_vez_de_dar_500(oficina, monkeypatch):
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))

    def _explota(_html):
        raise pdf_motor.SinMotor("no hay navegador")

    monkeypatch.setattr(pdf_motor, "desde_html", _explota)
    r = oficina.get("/informes/estado-cuenta/MWI/pdf")
    assert r.status_code == 503
    assert "navegador" in r.data.decode()
    # Y aclara que lo otro sigue andando: el usuario no tiene que adivinar si
    # se rompió todo o sólo esto.
    assert "impresi" in r.data.decode().lower()


# ---------------------------------------------------------------------------
# El botón: por dónde llega esto a WhatsApp en CADA aparato
# ---------------------------------------------------------------------------


def _boton(app, **ctx):
    from flask import render_template_string

    with app.test_request_context("/"):
        app.jinja_env.globals["pdf_disponible"] = lambda: True
        args = {"pdf_url": "/x/pdf",
                "wa_nombre": "LUIS ENRIQUE", "wa_clase": "btn", **ctx}
        con = ", ".join(f'{k}="{v}"' for k, v in args.items())
        return render_template_string(
            "{% with " + con + " %}"
            '{% include "informes/_ec_boton_whatsapp.html" %}{% endwith %}')


def test_el_boton_decide_por_el_TIPO_DE_APARATO_y_no_por_canShare(app):
    """⭐ El bug del 04/08, con la captura de la Mac de la dueña al lado:
    *"fijate porque no funciona whatsapp"*.

    La primera versión preguntaba `navigator.canShare({files})` y, si decía
    que sí, abría el menú de compartir del sistema. En el celular está bien:
    WhatsApp vive ahí. En una Mac, Safari TAMBIÉN dice que sí — y el menú que
    se abre tiene AirDrop, Mail, Messages, Notes y Freeform. WhatsApp no.
    O sea: un botón que decía "Enviar por WhatsApp" y abría un menú sin
    WhatsApp.

    El error fue preguntar "¿podés compartir?" cuando la pregunta era "¿por
    dónde llega esto a WhatsApp en ESTE aparato?".
    """
    html = _boton(app)
    assert "pointer: coarse" in html, "ya no distingue teléfono de escritorio"
    assert "web.whatsapp.com" in html, "en escritorio tiene que abrir WhatsApp Web"


def test_la_pestania_de_whatsapp_se_abre_en_el_click_y_no_despues(app):
    """Safari bloquea como popup cualquier `window.open` posterior a un
    `await`. Si se abriera después de generar el PDF, el botón bajaría el
    archivo sin abrir nada — medio botón, y encima en silencio."""
    html = _boton(app)
    antes_del_fetch = html.split("await fetch")[0]
    assert "window.open('', '_blank')" in antes_del_fetch


def test_el_destinatario_lo_elige_ella_en_whatsapp_y_no_el_programa(app):
    """⭐ Dueña 2026-08-05: *"¿podés solucionar que enviar por WhatsApp me deje
    seleccionar a quién?"* → *"a quien yo quiera de mis contactos"*, *"eso lo
    elijo en WhatsApp"*.

    La versión del 04/08 abría `wa.me/<telefono del cliente>`: caía adentro de
    esa conversación y le sacaba la elección de las manos. Y el estado de
    cuenta muchas veces NO va al cliente — va al contador del cliente, a un
    socio, a ella misma.

    Se descartó poner un selector propio (los números del cliente, un campo
    para tipear, un buscador): el selector bueno ya existe, tiene TODOS sus
    contactos y es el de WhatsApp. El programa deja WhatsApp abierto en la
    lista de chats y no elige por ella.
    """
    html = _boton(app)
    assert "web.whatsapp.com" in html
    # ⭐ El candado: NINGÚN link que salte a una conversación concreta. `wa.me`
    # puede quedar nombrado en los comentarios (explican por qué NO se usa),
    # así que se busca el link armado, que es lo que rompe la elección.
    assert "wa.me/' +" not in html
    assert "wa.me/{{" not in html
    assert "data-tel" not in html, "el botón ya no necesita el teléfono"
    # La ventana se abre siempre, sin condicionarla a nada.
    assert "var ventana = porMenu ? null : window.open" in html


def test_el_boton_no_cambia_de_ancho_mientras_genera(app):
    """El otro bug del 04/08: *"el generando mueve toda la pantalla, así que
    el nombre y los botones desordena diseño"*.

    "Enviar por WhatsApp" → "Generando…" son anchos distintos, y en una fila
    flex eso corre todo lo que está al lado. Se clava el ancho EXACTO (no un
    mínimo: el texto de espera puede ser más corto o más largo según la
    pantalla) y se suelta al terminar, incluso si algo falló.
    """
    html = _boton(app)
    assert "btn.style.width = btn.getBoundingClientRect().width" in html
    # Se suelta en el camino feliz Y en el finally: un botón que queda clavado
    # a 150px para siempre es peor que el salto que estamos arreglando.
    assert html.count("btn.style.width = ''") >= 2
    assert "finally" in html


def test_cada_pantalla_elige_su_texto_de_espera(app):
    """En el appbar del celular el botón es un cuadradito: con el ancho
    clavado, "Generando…" no entra. Por eso el texto es configurable."""
    assert "Generando" in _boton(app)
    assert 'data-cargando="···"' in _boton(app, wa_label="⤴", wa_label_cargando="···")


def test_sin_motor_de_pdf_no_se_dibuja_nada(app):
    from flask import render_template_string

    with app.test_request_context("/"):
        app.jinja_env.globals["pdf_disponible"] = lambda: False
        html = render_template_string(
            '{% with pdf_url="/x", wa_nombre="X", wa_clase="b" %}'
            '{% include "informes/_ec_boton_whatsapp.html" %}{% endwith %}')
    assert "data-wa-pdf" not in html


def test_el_arreglo_de_impresion_vale_para_TODAS_las_pantallas():
    """Dueña 2026-08-04: *"pero eso corregilo para todos los clientes y todos
    los métodos de impresión, ¿no?"*. Sí — y por eso las reglas de impresión
    viven en el PARCIAL, no en cada pantalla.

    `_estado_cuenta_impreso.html` es el cuerpo del estado de cuenta y lo
    incluyen las dos pantallas que existen: la individual y la de impresión en
    lote (por vendedor, provincia o grupos). Los tres botones de imprimir y el
    PDF salen de ahí. Si alguien arregla el papel tocando una sola pantalla,
    la otra queda distinta y no se nota hasta que está impreso — que es
    exactamente lo que pasó con el pie corrido.
    """
    from pathlib import Path

    base = Path("modules/informes/templates/informes")
    parcial = (base / "_estado_cuenta_impreso.html").read_text()

    # Las dos pantallas incluyen el mismo cuerpo.
    for pantalla in ("estado_cuenta.html", "estado_cuenta_lote_print.html"):
        assert "_estado_cuenta_impreso.html" in (base / pantalla).read_text(), pantalla

    # Y los arreglos del papel están en el parcial, no en una pantalla suelta.
    assert "@media print" in parcial
    for regla in ("main .ec-bloque-facturas table tbody td", ".ec-cierre"):
        assert regla in parcial, regla


def test_facturas_usa_toda_la_hoja_aunque_pierda_la_alineacion():
    """Reemplaza a `test_saldo_de_facturas_e_importe_de_cheques_terminan_igual`.

    El 04/08 la dueña pidió alinear la columna Saldo de facturas con Importe
    de cheques, y `c85c731b` lo resolvió con `table-layout: fixed` + anchos por
    `nth-child` en las DOS tablas. Este test verificaba esa suma.

    El 05/08 el pedido cambió, mirando el papel: *"ves que hay un espacio
    vacío a la derecha de facturas? debería usar todo el ancho de la hoja"* y
    *"sólo quería que imprima sin ese blanco y ANTES lo hacía bien"*.

    Por qué eran incompatibles: dos de las nueve columnas de facturas (Tipo y
    Stat) llevan `no-print` ⇒ `display:none`. Con ancho fijo desaparecen del
    layout pero siguen contando en el reparto, y en el motor del PDF la tabla
    quedaba en 590 px sobre 790 de hoja — un cuarto de página en blanco.
    CHEQUES, que no tiene columnas ocultas, ocupaba todo con las mismas reglas.

    Se prioriza usar la hoja entera. El test se DA VUELTA (no se borra): fija
    la decisión nueva y deja el porqué, para que nadie reponga los anchos
    creyendo que arregla la alineación.
    """
    import re
    from pathlib import Path

    css = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text().split("@media print", 1)[1]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    assert not re.search(r"\.ec-bloque-facturas table\s*\{[^}]*table-layout", css), (
        "facturas volvió a ancho fijo: vuelve el cuarto de hoja en blanco"
    )
    assert ".ec-bloque-facturas table th:nth-child" not in css, (
        "volvieron los anchos por nth-child de facturas"
    )
    # Cheques conserva el ancho fijo y sus anchos: ahí no hay columnas ocultas
    # y en el papel ocupa la hoja entera.
    assert "ec-bloque-cheques table { table-layout: fixed !important; }" in css
    anchos = {}
    for selectores, cuerpo in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"(?:^|[;\s])width:\s*([\d.]+)\s*%", cuerpo)
        if not m:
            continue
        for n in re.findall(r"\.ec-bloque-cheques table (?:th|tbody td):nth-child\((\d+)\)",
                            selectores):
            anchos[int(n)] = float(m.group(1))
    assert sum(anchos.values()) == 100, (
        f"los anchos de cheques ya no suman 100%: {sum(anchos.values())}"
    )
