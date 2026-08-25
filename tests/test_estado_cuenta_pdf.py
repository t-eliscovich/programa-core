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

from filters import telefonos, wa_tel
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
# El botón de LLAMAR de la ficha — un campo puede traer más de un número
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crudo, esperado",
    [
        # 🐞 El bug del 24/08: ATE tiene fijo y celular separados por un
        # espacio, y la ficha armaba UN link con los dos pegados
        # ("0325116760990010980"), que no es el número de nadie.
        ("032511676 0990010980", ["032511676", "0990010980"]),
        ("0991271637 0993391393", ["0991271637", "0993391393"]),
        # ⚠ Y el caso que prohíbe cortar por el espacio: UN número escrito
        # con espacios. Si esto se rompiera, la ficha ofrecería tres links
        # que marcan "0989", "506" y "447".
        ("0989 506 447", ["0989506447"]),
        ("099 123 4567", ["0991234567"]),
        ("0989506447", ["0989506447"]),
        ("032745123", ["032745123"]),
        # ⭐ Los dos que se comía cortar SÓLO por el largo (medidos sobre los
        # 476 clientes con saldo y teléfono: 14 casos, 1 marcando mal):
        # un número de 10 dígitos que no empieza en 09 salía con uno menos...
        ("2975005007", ["2975005007"]),
        # ...y un fijo de 7, escrito sin el código de área, no salía.
        ("3284800", ["3284800"]),
        ("0999999999 3284800", ["0999999999", "3284800"]),
        # ⭐ El GUIÓN va ADENTRO del número, no separa. Partir por "todo lo
        # que no es dígito" dejaba 2609 / 990 / 0986632533 y el celular se
        # perdía: la ficha ofrecía "260999009" para llamar, que no es de
        # nadie. Nueve clientes con saldo estaban así.
        ("2609-990 0986632533", ["2609990", "0986632533"]),
        ("2665-082 2647-267", ["2665082", "2647267"]),
        # La barra y la coma SÍ separan.
        ("032823944 /032821-888", ["032823944", "032821888"]),
        # Y lo que no es número se ignora sin llevarse el que sí lo es.
        ("072831997      EXT 4", ["072831997"]),
        # Basura: ningún link. Un botón que marca mal es peor que no tenerlo.
        ("", []), (None, []), ("s/n", []), ("123", []), ("0" * 20, []),
        # Cola incompleta: se queda con lo que SÍ es un número.
        ("0989506447 123", ["0989506447"]),
    ],
)
def test_telefonos(crudo, esperado):
    assert telefonos(crudo) == esperado


# ---------------------------------------------------------------------------
# El nombre del archivo — es lo primero que ve el cliente en el chat
# ---------------------------------------------------------------------------


def test_el_archivo_es_codigo_y_dia_y_nada_mas(monkeypatch):
    """TMT 2026-08-24: *"que el archivo que mando sea cod de cliente y día"*.

    El nombre largo del cliente NO va: con él adentro el archivo pasa de 30 a
    60 caracteres y WhatsApp lo muestra cortado justo por el final, que es
    donde está la fecha."""
    monkeypatch.setattr(estado_cuenta_pdf, "today_ec",
                        lambda: date(2026, 8, 24))
    assert estado_cuenta_pdf.nombre_archivo("MARIO W INNOVANOVENTA S.A", "mwi") == \
        "Estado de cuenta MWI 24-08-2026.pdf"


def test_la_fecha_del_archivo_va_sin_barras(monkeypatch):
    """Una barra en el nombre es un separador de carpetas: rompe el archivo en
    Android, en Windows y en el mail. Por eso dd-mm-aaaa y no dd/mm/aaaa."""
    monkeypatch.setattr(estado_cuenta_pdf, "today_ec",
                        lambda: date(2026, 1, 5))
    n = estado_cuenta_pdf.nombre_archivo("TDV", "TDV")
    assert "05-01-2026" in n and "/" not in n


def test_el_nombre_del_archivo_aguanta_cualquier_cosa(monkeypatch):
    """Pasa por WhatsApp, por mail y por el disco de quien lo reciba."""
    monkeypatch.setattr(estado_cuenta_pdf, "today_ec",
                        lambda: date(2026, 8, 24))
    # El nombre del cliente no entra, ni siquiera si viene con basura.
    assert estado_cuenta_pdf.nombre_archivo("Núñez & Hijos S.A", "NUN") == \
        "Estado de cuenta NUN 24-08-2026.pdf"
    # Tildes y signos del CÓDIGO: fuera, no todos los sistemas los bancan en
    # un nombre de archivo, y una barra rompería la ruta al guardarlo.
    assert estado_cuenta_pdf.nombre_archivo("", "a/b") == \
        "Estado de cuenta AB 24-08-2026.pdf"
    assert "/" not in estado_cuenta_pdf.nombre_archivo("A/B: C", "a/b")
    # Sin código tampoco queda anónimo.
    assert estado_cuenta_pdf.nombre_archivo("", "") == \
        "Estado de cuenta CLIENTE 24-08-2026.pdf"
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
    # Y el archivo llega con el código, el nombre del cliente y el día: el
    # nombre es lo que el cliente ve en el chat antes de abrirlo, y el código
    # y la fecha son lo que lo distingue de los otros cuatro que se mandaron
    # esa tarde.
    for r in (a, b):
        cd = r.headers["Content-Disposition"]
        assert "MWI" in cd and cd.endswith('.pdf"')
        # El nombre largo del cliente NO: TMT 2026-08-24.
        assert "MARIO W INNOVANOVENTA" not in cd


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
    # El ancla es el pedido del PDF: la ventana tiene que abrirse ANTES.
    antes_del_pdf = html.split("prepararPdf(btn).then")[0]
    assert "prepararPdf(btn).then" in html, "cambió el nombre del ancla"
    assert "window.open('', '_blank')" in antes_del_pdf


# ---------------------------------------------------------------------------
# 🚨 El bug de Android del 20/08: "abrió el PDF, no WhatsApp"
# ---------------------------------------------------------------------------


def test_el_pdf_se_empieza_a_preparar_cuando_el_dedo_APOYA(app):
    """⭐ TMT 2026-08-20, probándolo en un Android: *"pero no abre bien whatsapp
    lo probe"* → abrió el visor de PDF.

    El menú de compartir del sistema sólo se abre con el permiso que deja el
    toque, y ese permiso dura unos segundos. Este PDF tarda entre 3 y 4,7 s en
    generarse (medido en producción): el permiso se vence MIENTRAS se genera,
    `navigator.share` explota y el código caía al plan B de escritorio, que
    baja el archivo — el teléfono lo abre en el visor y de WhatsApp, nada.

    El arreglo no es reintentar: es no tener nada que esperar en el momento del
    toque. Por eso el PDF arranca en `pointerdown`, que pasa antes del click.
    """
    html = _boton(app)
    assert "pointerdown" in html, "el PDF no se adelanta al toque"
    apoyar = html.split("addEventListener('pointerdown'")[1].split("}")[0]
    assert "prepararPdf" in apoyar


def test_con_el_pdf_ya_hecho_el_compartir_sale_sin_esperar_NADA(app):
    """El candado del bug: entre el click y `navigator.share` no puede haber
    una sola espera, o se pierde el permiso del toque otra vez."""
    html = _boton(app)
    compartir = html.split("function compartirYa")[1].split("\n  function ")[0]
    assert "navigator.share" in compartir
    assert "await" not in compartir, "volvió a esperar antes de compartir"
    # Y el click usa ese camino cuando el PDF ya está en memoria.
    assert "if (porMenu && btn.__listo) { if (compartirYa(btn, btn.__listo)) return; }" in html


def test_si_el_permiso_se_vencio_el_boton_pide_UN_SEGUNDO_TOQUE(app):
    """Cuando la generación tardó de más, el PDF igual quedó en memoria: el
    botón lo dice y el toque siguiente abre el menú al instante. Nunca se cae
    al camino de escritorio, que en un teléfono termina en el visor de PDF."""
    html = _boton(app)
    assert "'Enviar ahora'" in html
    # El fallback de escritorio (bajar + WhatsApp Web) queda para el escritorio.
    telefono = html.split("function compartirYa")[1].split("function largo")[0]
    assert "web.whatsapp.com" not in telefono


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
    assert "var ventana = (porMenu || esTel) ? null : window.open" in html


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


def test_los_anchos_de_las_dos_tablas_suman_100_sobre_lo_que_SE_IMPRIME():
    """Lo único que le da ancho a una tabla en el motor de PDF.

    En ese motor el `width:100%` de la tabla no se aplica: lo único que la
    estira es `table-layout: fixed` con porcentajes que sumen 100. Y tienen
    que sumar 100 **sobre las columnas que se imprimen**: Tipo (3) y Stat (8)
    de facturas son `no-print` ⇒ `display:none`, no existen en el papel, así
    que si se les declara un ancho ese porcentaje se pierde y la tabla sale
    corta. Cheques nunca tuvo el problema porque imprime sus 8 columnas.

    Costó cinco vueltas (dueña: "facturas tiene que ocupar todo
    horizontalmente"). En el navegador se ve bien igual — por eso el test mira
    la SUMA en el CSS y no el render.
    """
    import re
    from pathlib import Path

    css = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text().split("@media print", 1)[1]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    anchos = {"facturas": {}, "cheques": {}}
    for selectores, cuerpo in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"(?:^|[;\s])width:\s*([\d.]+)\s*%", cuerpo)
        if not m:
            continue
        for bloque, n in re.findall(
                r"\.ec-bloque-(facturas|cheques) table (?:th|tbody td)"
                r":nth-child\((\d+)\)", selectores):
            anchos[bloque][int(n)] = float(m.group(1))
        for n in re.findall(r"\.ec-bloque-facturas table \.(ec-c-[a-z]+)", selectores):
            anchos["facturas"][n] = float(m.group(1))

    for bloque in ("facturas", "cheques"):
        assert f"ec-bloque-{bloque} table {{ table-layout: fixed !important; }}" in css, (
            f"{bloque} sin table-layout fijo: en el PDF sale corta"
        )
        total = sum(anchos[bloque].values())
        assert total == 100, (
            f"los anchos de {bloque} suman {total}%, no 100 ⇒ la tabla sale "
            f"corta en el PDF (en el navegador se ve bien igual)"
        )
    # Y a las columnas que no se imprimen no se les declara ancho: si se les
    # declara, su porcentaje se pierde del reparto.
    # Y facturas reparte por NOMBRE de columna, no por posición: tiene 9
    # columnas en pantalla y 7 en el papel, así que `nth-child` cae en la
    # columna equivocada según de dónde salga la hoja.
    assert not any(isinstance(k, int) for k in anchos["facturas"]), (
        "facturas volvió a repartir por nth-child"
    )


def test_dias_no_se_come_un_sexto_de_la_hoja():
    """Que la tabla mida 100% no alcanza: hay que USAR el ancho.

    Con DÍAS en 16% las líneas llegaban al borde pero los números quedaban
    apretados a la izquierda y sobraba un hueco muerto a la derecha — una
    columna de dos dígitos no necesita un sexto de la hoja. Dueña:
    "facturas tiene que ocupar todo horizontalmente".
    """
    import re
    from pathlib import Path

    css = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text().split("@media print", 1)[1]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    anchos = {}
    for selectores, cuerpo in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"(?:^|[;\s])width:\s*([\d.]+)\s*%", cuerpo)
        if not m:
            continue
        for c in re.findall(r"\.ec-bloque-facturas table \.(ec-c-[a-z]+)", selectores):
            anchos[c] = float(m.group(1))
    assert anchos["ec-c-dias"] <= 10, (
        f'Días en {anchos["ec-c-dias"]}%: deja un hueco muerto a la derecha')
    for c in ("ec-c-imp", "ec-c-abo", "ec-c-sal", "ec-c-acum"):
        assert anchos[c] >= anchos["ec-c-dias"], f"{c} más angosta que Días"


def test_las_columnas_que_no_se_imprimen_estan_anuladas_en_las_8_celdas():
    """El último 12% que le faltaba a facturas para llenar la hoja.

    `no-print` no alcanzaba: en la fila "Totalizada" la celda de Stat ni
    siquiera lo llevaba, así que la columna existía igual. Con
    `table-layout: fixed`, una columna que existe sin ancho declarado toma su
    mínimo de contenido y ese espacio sale de las otras — la tabla quedaba en
    88% de la hoja.
    """
    from pathlib import Path

    tpl = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text()
    # 2 th + 2 celdas de la fila normal + 2 de la fila "Totalizada" + 2 del pie
    # Las 8 celdas de las dos columnas ocultas + la regla CSS de respaldo.
    assert tpl.count("ec-c-off") == 8 + 1
    # Y NINGUNA se genera en el papel: van detrás de {% if interactivo %}.
    # Medido con pdfplumber: escondidas con `no-print` seguían ocupando 32 pt
    # cada una (64,5 pt de tabla vacía a la derecha) porque el `display:none`
    # no se les aplica en el motor de PDF.
    import re
    for m in re.finditer(r'<t[dh] class="ec-c-off', tpl):
        antes = tpl[:m.start()].rstrip()
        assert antes.endswith("{% if interactivo %}"), (
            "una celda de columna oculta no está gateada por `interactivo`: "
            "en el papel ocupa ancho aunque no se vea"
        )
    css = tpl.split("@media print", 1)[1]
    assert "main .ec-bloque-facturas table .ec-c-off {" in css
    for prop in ("display: none !important", "width: 0 !important",
                 "padding: 0 !important"):
        assert prop in css, prop


def test_el_ancho_de_facturas_va_en_el_HTML_no_solo_en_el_CSS():
    """Las reglas del `@media print` no le llegan a esa tabla en el motor de PDF.

    Medido sobre la hoja impresa: las columnas tenían ancho de CONTENIDO, no
    los porcentajes declarados. Siete intentos de reajustar porcentajes en el
    CSS no cambiaron nada. El ancho va en el HTML —`<colgroup>` y
    `table-layout: fixed` inline— en la versión no interactiva (papel/PDF).
    """
    import re
    from pathlib import Path

    tpl = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text()
    bloque = tpl[tpl.index("ec-bloque-facturas"):tpl.index("</thead>")]
    assert bloque.count("</colgroup>") == 1, "tiene que haber UN solo colgroup"
    assert bloque.count("<table ") == 1, (
        "UNA sola tabla para pantalla y papel. Con dos ramas, un arreglo entra "
        "en una y no en la otra y no se nota hasta que se imprime la hoja."
    )
    assert 'style="table-layout: fixed; width: 100%; min-width: 100%;"' in bloque, (
        "sin `min-width: 100%` la tabla se queda en la SUMA de sus columnas "
        "(el width:100% no se resuelve en el motor de PDF) y el ancho queda "
        "atado al tamaño de papel con el que se calcularon los pt"
    )
    anchos = [float(x) for x in re.findall(r'<col[^>]*style="width:([\d.]+)pt"', bloque)]
    assert len(anchos) == 7, (
        f"el colgroup tiene {len(anchos)} columnas; en el papel la tabla tiene 7 "
        "(Tipo y Stat no se generan)"
    )
    # En PUNTOS, no en porcentaje: un % se resuelve contra el ancho de la tabla,
    # que en el motor de PDF no llega a 100% — medido, 473,2 de 537,7.
    # Con `table-layout: fixed` la tabla crece hasta la suma de sus columnas.
    # Los pt fijan la PROPORCIÓN y sirven de piso; el ancho final lo pone el
    # `min-width: 100%`. No pueden pasarse del útil del papel más angosto que
    # se use (A4 = 537,7 pt), o desbordan si el min-width no resolviera.
    # LA SUMA ES EL ANCHO DE LA TABLA: en el motor de PDF ni `width:100%` ni
    # `min-width:100%` se resuelven (los dos probados inline y medidos). 554,0
    # es el útil de la hoja Letter con los márgenes actuales.
    # En vw, que se resuelve contra el ancho de la PÁGINA: así el mismo número
    # sirve para A4 (útil 92,4%) y Letter (92,6%). Ni `%` (circular, se resuelve
    # contra la tabla) ni `pt` (atado al papel) funcionan — los dos probados.
    # La suma ES el ancho de la tabla (ni width:100% ni min-width:100% ni %
    # ni vw se resuelven en este motor — los cuatro probados y medidos).
    # A4 = 595 pt, `@page` deja 8 mm por lado ⇒ 595 − 2×22,68 = 549,6 útiles.
    total = round(sum(anchos), 1)
    assert 546 <= total <= 549.6, (
        f"los <col> suman {total} pt; el útil de A4 con margen 8 mm es 549,6"
    )
    # Y el papel tiene que estar FIJO: si no, el navegador imprime A4 y el
    # endpoint /pdf sale Letter, y un ancho absoluto no puede servir a los dos.
    css = Path("modules/informes/templates/informes/"
               "_estado_cuenta_impreso.html").read_text()
    assert "@page { size: A4; margin: 7mm 8mm; }" in css, (
        "sin `size: A4` hay dos salidas con anchos distintos"
    )
    # Y la proporción tiene que ser la acordada: Días es la más angosta.
    assert anchos[-1] == min(anchos)


# ---------------------------------------------------------------------------
# 🚨 El bug del 25/08: "el click no hace nada" (Android, usuario de Patricio)
# ---------------------------------------------------------------------------


def test_el_camino_rapido_no_puede_TIRAR_y_dejar_el_boton_mudo(app):
    """⭐ TMT 2026-08-25: *"está trabado enviar por whatsapp a vendedores"* →
    *"el click no hace nada"*.

    Que el rótulo ni siquiera cambie a "Generando…" significa que el toque
    murió antes de la primera línea visible. `new File(...)` y
    `navigator.canShare` no existen en todos los navegadores de Android y ahí
    TIRAN — y se llamaban sin red en el camino rápido, que es el que corre del
    segundo toque en adelante.
    """
    html = _boton(app)
    compartir = html.split("function compartirYa")[1].split("\n  function ")[0]
    assert "try {" in compartir, "el camino rápido volvió a quedar sin red"
    assert "new File(" in compartir
    # Y cuando no se puede compartir, se baja el archivo y se avisa: el botón
    # puede no poder, pero no puede quedarse callado.
    assert "bajarYExplicar" in compartir


def test_si_share_explota_de_entrada_igual_avisa(app):
    """`navigator.share` puede tirar en vez de devolver una promesa: el
    `.catch` quedaba colgado de un `undefined` y el toque moría en silencio."""
    html = _boton(app)
    compartir = html.split("function compartirYa")[1].split("\n  function ")[0]
    despues = compartir.split("navigator.share(")[1]
    assert "} catch (e) {" in despues
    assert "bajarYExplicar" in despues


def test_el_click_no_se_cae_si_el_toque_no_trae_elemento(app):
    """El manejador de `pointerdown` guardaba `closest`; el del click no. Un
    toque sin elemento se llevaba puesto todo el manejador."""
    html = _boton(app)
    clic = html.split("addEventListener('click'")[1]
    assert "ev.target && ev.target.closest && ev.target.closest('[data-wa-pdf]')" in clic


def test_un_telefono_sin_menu_de_compartir_no_va_a_whatsapp_web(app):
    """WhatsApp Web en un celular es una pantalla que dice "abrilo en la
    computadora". Si el teléfono no tiene menú de compartir, el plan B es bajar
    el PDF y explicarlo — no mandarlo a una pared."""
    html = _boton(app)
    largo = html.split("function largo")[1].split("addEventListener('click'")[0]
    assert "if (esTel) { bajarYExplicar(listo); return; }" in largo
    assert "var ventana = (porMenu || esTel) ? null : window.open" in largo
