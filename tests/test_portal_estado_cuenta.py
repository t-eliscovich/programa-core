"""El estado de cuenta que ve el cliente en el portal (desde el 04/09/2026,
el INICIO: rediseño a pedido de la dueña, *"pensemos todo lo que hace una
aplicación user friendly… hacelo lindo"*; ver `portal/_app.html`).

TMT 2026-08-24. Dos reglas, y las dos son de las que se rompen en silencio:

⭐ **Los números salen de la MISMA función que usa la oficina**
(`informes.queries.estado_cuenta_cliente`). El portal no calcula nada: si el
saldo que ve el cliente saliera de otra cuenta, tarde o temprano diría algo
distinto que el que ve la oficina — y el que llama por teléfono es él.

⭐ **La hoja para imprimir es el MISMO documento.** El cuerpo sale del parcial
compartido `informes/_estado_cuenta_impreso.html`. Dos plantillas del mismo
documento divergen a la primera corrección: ya pasó con el papel que el
vendedor le deja al cliente, y por eso `mi_cartera` tampoco arma la suya.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TPL = ROOT / "modules" / "portal" / "templates" / "portal"
PANTALLA = (TPL / "inicio.html").read_text(encoding="utf-8")
APP = (TPL / "_app.html").read_text(encoding="utf-8")
HOJA = (TPL / "estado_cuenta_impreso.html").read_text(encoding="utf-8")
VISTAS = (ROOT / "modules" / "portal" / "views.py").read_text(encoding="utf-8")
HOJA_PORTAL = HOJA


def _sin_comentarios(texto: str) -> str:
    """Fuera los `{# ... #}`.

    🚨 Los comentarios de estas plantillas explican JUSTAMENTE lo que el test
    prohíbe ("no usa base.html", "copia del bloque de estilos"), así que
    buscar el texto suelto se encuentra a sí mismo. Ya me pasó dos veces hoy.
    """
    return re.sub(r"\{#.*?#\}", "", texto, flags=re.S)


def test_los_numeros_salen_de_la_funcion_de_la_oficina():
    assert "from modules.informes import queries" in VISTAS
    assert "queries.estado_cuenta_cliente" in VISTAS or "_q.estado_cuenta_cliente" in VISTAS


def test_el_portal_no_escribe_ni_una_consulta_de_plata():
    """Si acá apareciera un SELECT sobre `factura` o `cheque`, sería una
    segunda cuenta corriendo en paralelo a la de la oficina."""
    from modules.portal import views
    fuente = Path(views.__file__).read_text(encoding="utf-8")
    for tabla in ("scintela.factura", "scintela.cheque", "SUM("):
        assert tabla not in fuente, f"el portal está calculando plata por su cuenta ({tabla})"


def test_la_hoja_para_imprimir_incluye_el_parcial_compartido():
    assert '{% include "informes/_estado_cuenta_impreso.html" %}' in HOJA


def test_la_hoja_del_portal_usa_la_misma_css_que_la_oficina():
    """🚨 La CSS de impresión está COPIADA de
    `informes/estado_cuenta_lote_print.html` porque los tres envoltorios tienen
    la suya y unificarlos es una sesión aparte sobre una hoja que llevó ocho
    vueltas de ajuste.

    Copiada no puede querer decir "y que se separen solas": este test compara
    los dos textos. Si alguien ajusta el ancho de una columna en la de la
    oficina y no acá, el papel del cliente sale distinto y nadie se entera
    hasta que la dueña compara dos impresiones.
    """
    lote = (ROOT / "modules" / "informes" / "templates" / "informes"
            / "estado_cuenta_lote_print.html").read_text(encoding="utf-8")
    portal = (TPL / "_hoja_css.html").read_text(encoding="utf-8")

    # ⚠ Desde el 25/08 la hoja de la oficina tiene DOS bloques `<style>`: el de
    # impresión y el de `{% if imagen %}`, que sólo corre cuando la hoja se
    # saca como foto para WhatsApp. Este test vigila el PAPEL, así que se
    # queda con el bloque que trae `@media print` y no con "el primero" —
    # agarrar el primero comparaba el de la imagen contra el del portal y daba
    # rojo sin que nadie hubiera tocado la hoja impresa.
    de_la_oficina = next(
        b for b in re.findall(r"<style>(.*?)</style>", _sin_comentarios(lote), re.S)
        if "@media print" in b)
    del_portal = re.search(r"<style>(.*?)</style>",
                           _sin_comentarios(portal), re.S).group(1)
    assert del_portal.strip() == de_la_oficina.strip(), (
        "la CSS de impresión del portal se separó de la de la oficina: el "
        "papel del cliente ya no sale igual que el de la oficina")


def test_el_envoltorio_del_portal_no_usa_el_chrome_del_erp():
    """El de la oficina extiende `base.html`, que trae el menú del ERP y un
    breadcrumb con `url_for('informes.estado_cuenta_landing')` — dos cosas que
    en este proceso no existen y que tirarían BuildError."""
    codigo = _sin_comentarios(HOJA)
    assert "base.html" not in codigo
    assert "url_for(" not in codigo


def test_el_saldo_a_favor_se_muestra_como_a_favor():
    """Un saldo negativo con un signo menos adelante lo lee mal cualquiera: un
    cliente que ve '-$ 500' llama preguntando qué le debe.

    Se resuelve cambiando el RÓTULO y mostrando el número en positivo, que es
    como lo diría una persona: 'saldo a favor suyo, $ 500'."""
    assert "Saldo a favor suyo" in PANTALLA
    assert "_saldo|abs|money_es" in PANTALLA, (
        "el número tiene que ir en positivo; el signo lo dice el rótulo")


def test_hoy_es_una_FECHA_y_no_un_texto():
    """🚨 El parcial compartido hace `vencimiento < hoy` para pintar las
    vencidas. `factura.vencimiento` viene como `date` de la base: comparar un
    date con un string no da False, **LEVANTA**, y se cae la pantalla entera.

    Y el nombre tiene que ser `hoy`, el mismo que usa el portal de vendedores,
    porque el parcial es el mismo."""
    assert '"hoy": today_ec(),' in VISTAS
    assert "date.today().strftime" not in VISTAS
    assert "vence < hoy" in MOVIMIENTOS or "< hoy %}" in MOVIMIENTOS


def test_los_numeros_van_en_formato_de_ecuador():
    """🔢 Punto de miles, coma de decimales: `2.812,86`, no `2812.86`.

    Los filtros de la casa (`money_es`, `fecha_es`) se registran en
    `create_app` ANTES de elegir el modo, así que el portal los tiene. Formatear
    a mano con `'%.2f'|format` sale en formato yanqui — y salió, en la primera
    corrida contra producción."""
    assert "'%.2f'|format" not in PANTALLA, (
        "hay un número formateado a mano: va a salir en formato yanqui")
    assert "money_es" in PANTALLA


MOVIMIENTOS = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
               / "_movimientos.html").read_text(encoding="utf-8")


def test_las_fechas_van_por_el_filtro_de_la_casa():
    """`strftime` se cae con un `None` y con un texto ISO. `fecha_es` aguanta
    los tres casos, que es justamente por lo que existe.

    Las fechas de las facturas viven en el parcial compartido, que ya las
    formatea así; acá se cuida que la pantalla del portal no meta las suyas."""
    for pantalla in ("inicio", "facturas", "pagos", "despachos", "despacho", "factura",
                     "_fila_factura", "_fila_pago", "_fila_despacho"):
        t = (TPL / f"{pantalla}.html").read_text(encoding="utf-8")
        assert "strftime" not in t, pantalla
        assert "'%.2f'|format" not in t, pantalla
    assert "fecha_es" in MOVIMIENTOS


BASE_PORTAL = (TPL / "base.html").read_text(encoding="utf-8")


def test_el_portal_usa_las_variables_de_la_casa_y_no_colores_sueltos():
    """Si el portal escribiera `#e30613` a mano, cambiar el color de la marca
    en un lado no lo cambiaría en el otro."""
    import re
    for pantalla in (BASE_PORTAL, PANTALLA):
        estilos = "\n".join(re.findall(r"<style>(.*?)</style>", pantalla, re.S))
        # Las variables se definen UNA vez, en :root; afuera de ahí, nada.
        estilos = re.sub(r":root\{[^}]*\}", "", estilos)
        sueltos = [c for c in re.findall(r"#[0-9a-fA-F]{6}\b", estilos)]
        assert not sueltos, f"colores escritos a mano: {sueltos}"
    assert "var(--accent)" in BASE_PORTAL
    # La puerta y el adentro comparten la cara: mismo rojo, escrito una vez.
    assert BASE_PORTAL.count("#E30613") == 1 and "--accent:var(--rojo)" in BASE_PORTAL
    # El armazón nuevo define las variables UNA vez (en :root) y el resto va
    # por var(--…): el rojo de la marca está escrito una sola vez.
    estilos = "\n".join(re.findall(r"<style>(.*?)</style>", APP, re.S))
    assert estilos.count("#E30613") == 1
    assert "var(--rojo)" in estilos


def test_el_archivo_se_llama_igual_que_el_de_la_oficina():
    """`Estado de cuenta ATE 24-08-2026.pdf` — el código y el día. Cinco
    estados de cuenta en una carpeta tienen que distinguirse sin abrirlos, y
    ese nombre ya se decidió una vez: se usa la MISMA función."""
    assert "estado_cuenta_pdf.nombre_archivo" in VISTAS


def test_el_pdf_no_usa_la_funcion_de_la_oficina_y_esta_bien():
    """🚨 `estado_cuenta_pdf.generar()` renderiza
    `informes/estado_cuenta_lote_print.html`, que extiende el chrome del ERP y
    llama a `url_for('informes.…')`. En el proceso del portal esas rutas NO
    existen: el PDF moriría con un BuildError, igual que pasaba con la página
    de 404.

    Por eso arma el HTML con el envoltorio del portal — que incluye el mismo
    parcial y la misma CSS de impresión (lo cuida
    `test_la_hoja_del_portal_usa_la_misma_css_que_la_oficina`)."""
    # 🚨 Por AST y no buscando el texto: el docstring de la vista NOMBRA
    # `estado_cuenta_pdf.generar()` justamente para explicar por qué no se usa,
    # así que el assert de texto se encuentra a sí mismo. Me pasó cinco veces
    # en esta sesión; por AST no hay forma.
    import ast

    arbol = ast.parse(VISTAS)
    llamadas = {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }
    assert "estado_cuenta_pdf.generar" not in llamadas, (
        "el portal está llamando a la función de la oficina: renderiza el "
        "chrome del ERP y muere con BuildError en este proceso")
    assert "pdf_motor.desde_html" in llamadas
    assert 'render_template("portal/estado_cuenta_impreso.html"' in VISTAS


def test_sin_motor_de_pdf_contesta_algo_que_se_entiende():
    """El botón se esconde cuando no hay motor, pero alguien puede llegar por
    la URL. Un 503 con un mensaje que dice qué pasa evita el reporte de "no
    anda el botón"."""
    assert "pdf_motor.SinMotor" in VISTAS
    assert "status=503" in VISTAS


def test_el_boton_solo_aparece_si_hay_con_que_generarlo():
    """Un botón que contesta 503 enseña a no confiar en los botones. Se usa el
    mismo `pdf_disponible()` que la ficha del vendedor."""
    assert "{% if pdf_disponible() %}" in PANTALLA


def test_dentro_del_PDF_no_va_el_boton_de_imprimir():
    """Nadie va a tocar un botón adentro de un archivo."""
    assert "{% if not para_pdf|default(false) %}" in HOJA_PORTAL
    assert "para_pdf=True" in VISTAS


def test_la_hoja_para_imprimir_sale_aunque_el_cliente_tenga_cheques(monkeypatch):
    """🐞 04/09/2026, con AJT (54 cheques): la hoja y el PDF daban 500. El
    parcial compartido linkea cada cheque a `cheques.detalle` salvo para un
    vendedor; en el portal no hay usuario y esa ruta no existe → BuildError.
    Se prueba por la PANTALLA, con un cheque de verdad en la data."""
    import datetime as dt
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    try:
        from modules.informes import queries as q
        hoy = dt.date(2026, 9, 4)
        cheque = {"id_cheque": 7, "no_cheque": "0001840", "fecha": hoy, "fechad": hoy,
                  "fechaing": hoy, "fecha_recibido": hoy, "fecha_crea": hoy,
                  "fechaout": None, "dia_ingreso": hoy, "fechad_original": None,
                  "fecha_postergacion": None, "importe": 3488.89, "stat": "Z",
                  "banco": "PICHINCHA", "nombre_banco": "PICHINCHA", "no_banco": 10,
                  "por_cobrar": True}
        monkeypatch.setattr(q, "estado_cuenta_cliente", lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "TOTOY", "ruc": "1724354004001"},
            "facturas": [], "cheques": [cheque], "anticipos": [],
            "totales": q.totales_estado_cuenta_en_cero(),
        })
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "AJT"
        with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
            r = c.get("/estado-de-cuenta/imprimir")
        assert r.status_code == 200, r.status_code
        html = r.get_data(as_text=True)
        assert "0001840" in html
        assert "/cheques/" not in html
    finally:
        deshacer()


# ---------------------------------------------------------------------------
# El rediseño del 04/09/2026
# ---------------------------------------------------------------------------

PANTALLAS_DE_ADENTRO = ("inicio", "facturas", "pagos", "despachos", "despacho", "factura")


def test_todas_las_pantallas_de_adentro_usan_el_mismo_armazon():
    """Una barra de navegación visible SIEMPRE (NN/g: los menús escondidos
    bajan un 21% la tarea completada), la misma en las seis pantallas, y en
    la computadora un menú arriba en vez de la barra."""
    for pantalla in PANTALLAS_DE_ADENTRO:
        t = (TPL / f"{pantalla}.html").read_text(encoding="utf-8")
        assert '{% extends "portal/_app.html" %}' in t, pantalla
        assert "{% block seccion %}" in t, pantalla
    assert 'class="tabbar"' in APP and 'class="topnav"' in APP
    for destino in ("/", "/facturas", "/mis-pagos", "/despachos"):
        assert APP.count(f'href="{destino}"') >= 2, destino   # barra + menú


def test_el_armazon_no_depende_de_los_estilos_del_vendedor():
    """Dueña 04/09: *"aunque tardemos cambiemos el approach"*. El portal dejó
    de compartir la hoja de Mi Cartera (pantallas de trabajo, tablas de siete
    columnas); lo que sigue compartido es lo que dice números."""
    assert "mi_cartera/_estilos.html" not in APP
    assert "tailwind" not in APP


def test_el_salir_del_armazon_es_un_svg_y_no_un_caracter():
    """🚨 Regla ya pagada dos veces (el emoji de WhatsApp el 20/08, el ⎙ el
    24/08): en un botón no va un carácter especial, va un SVG."""
    boton = re.search(r'<a class="salir".*?</a>', APP, re.S).group(0)
    assert "<svg" in boton
    assert not re.search(r">[^<\s]", boton.replace("</svg>", "")), "texto suelto en el botón"
    for a in re.findall(r'<a href="[^"]*" class="[^"]*">(.*?)</a>', APP[APP.index('class="tabbar"'):], re.S):
        assert "<svg" in a


def test_el_inicio_tiene_UN_boton_y_avisa_lo_vencido_y_lo_proximo():
    """Lo que importa, primero y solo: el saldo, si hay algo vencido, el
    próximo vencimiento en una frase, y UNA acción (el PDF; la hoja en su
    lugar si no hay motor)."""
    assert PANTALLA.count('href="/estado-de-cuenta.pdf"') == 1
    assert PANTALLA.count('href="/estado-de-cuenta/imprimir"') == 1
    assert "{% if pdf_disponible() %}" in PANTALLA
    assert "n_vencidas" in PANTALLA and "proximo" in PANTALLA
    assert "Ninguna factura vencida" in PANTALLA
    # Pagos y despachos se llegan por el menú y por los "Ver todos" de las
    # listas, no por botones sueltos.
    assert "Ver todos" in PANTALLA


def test_los_estados_hablan_el_idioma_del_cliente():
    """Nielsen 2: ni STAT ni ACUM. ni RETENC. — "al día", "vence en 12 días",
    "vencida hace 3 días", "a su favor"."""
    from datetime import date

    from modules.portal import presentacion as pr
    hoy = date(2026, 9, 4)
    assert pr.estado_de_factura({"importe": 100, "vencimiento": date(2026, 11, 1)}, hoy)["texto"] == "al día"
    assert pr.estado_de_factura({"importe": 100, "vencimiento": date(2026, 9, 16)}, hoy)["texto"] == "vence en 12 días"
    assert pr.estado_de_factura({"importe": 100, "vencimiento": date(2026, 9, 5)}, hoy)["texto"] == "vence en 1 día"
    assert pr.estado_de_factura({"importe": 100, "vencimiento": hoy}, hoy)["texto"] == "vence hoy"
    e = pr.estado_de_factura({"importe": 100, "vencimiento": date(2026, 9, 1)}, hoy)
    assert e["texto"] == "vencida hace 3 días" and e["clase"] == "bad"
    assert pr.estado_de_factura({"importe": -50, "vencimiento": date(2026, 9, 1)}, hoy)["texto"] == "a su favor"
    assert pr.estado_de_factura({"importe": 100, "vencimiento": None}, hoy)["clase"] == "ok"
    for pantalla in PANTALLAS_DE_ADENTRO + ("_fila_factura", "_fila_pago"):
        t = (TPL / f"{pantalla}.html").read_text(encoding="utf-8")
        for jerga in ("STAT", "ACUM", "RETENC."):
            assert jerga not in t, (pantalla, jerga)


def test_el_proximo_vencimiento_es_el_mas_cercano_con_saldo_y_no_vencido():
    from datetime import date

    from modules.portal import presentacion as pr
    hoy = date(2026, 9, 4)
    fs = [
        {"numf": 1, "importe": 100, "saldo": 100, "vencimiento": date(2026, 9, 1)},   # vencida: no
        {"numf": 2, "importe": 100, "saldo": 0, "vencimiento": date(2026, 9, 6)},     # sin saldo: no
        {"numf": 3, "importe": -100, "saldo": -100, "vencimiento": date(2026, 9, 5)}, # a favor: no
        {"numf": 4, "importe": 100, "saldo": 40, "vencimiento": date(2026, 9, 20)},
        {"numf": 5, "importe": 100, "saldo": 40, "vencimiento": date(2026, 9, 10)},
    ]
    p = pr.proximo_vencimiento(fs, hoy)
    assert p["factura"]["numf"] == 5 and p["dias"] == 6
    assert pr.proximo_vencimiento(fs[:3], hoy) is None


def test_las_listas_van_por_mes_del_mas_nuevo_al_mas_viejo():
    from datetime import date

    from modules.portal import presentacion as pr
    items = [{"fecha": date(2026, 9, 2), "saldo": 10, "id": 1},
             {"fecha": date(2026, 8, 30), "saldo": 5, "id": 2},
             {"fecha": date(2026, 9, 2), "saldo": 1, "id": 3},
             {"fecha": "2026-08-01", "saldo": 2, "id": 4}]   # Asinfo manda texto
    orden = pr.ordenar_por_fecha(items, "fecha", "id")
    assert [x["id"] for x in orden] == [3, 1, 2, 4]
    grupos = pr.por_mes(orden, "fecha", "saldo")
    assert [(g["mes"], len(g["items"]), g["total"]) for g in grupos] == [
        ("Septiembre 2026", 2, 11.0), ("Agosto 2026", 2, 7.0)]


def _app_portal():
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        return build_app()


def _data(monkeypatch, facturas=(), cheques=()):
    from modules.informes import queries as q
    monkeypatch.setattr(q, "estado_cuenta_cliente", lambda cod: {
        "cliente": {"codigo_cli": cod, "nombre": "TOTOY BUITRON ANDRES JULIO",
                    "ruc": "1724354004001", "vend": "EDG"},
        "facturas": list(facturas), "cheques": list(cheques), "anticipos": [],
        "totales": {**q.totales_estado_cuenta_en_cero(), "saldo": 735.25, "saldo_neto": 735.25},
    })
    from modules.portal import views
    monkeypatch.setattr(views, "_vendedor_de", lambda fic: {
        "codigo": "EDG", "nombre": "Edgar Ramirez", "iniciales": "ER", "correo": ""})
    monkeypatch.setattr(views, "_despachos_recientes", lambda cod, ruc: [])


def _factura(**kw):
    import datetime as dt
    base = {"numf": 183341, "numf_completo": "001-099-000183341", "id_factura": 9,
            "fecha": dt.date(2026, 9, 2), "vencimiento": dt.date(2026, 12, 1),
            "importe": 735.25, "saldo": 735.25, "abono": 0, "retencion": 0, "stat": "Z"}
    return {**base, **kw}


def test_el_inicio_se_dibuja_con_datos_de_verdad(monkeypatch):
    """Por la PANTALLA: saldo, chip, próximo vencimiento, la factura como
    tarjeta con su estado, el vendedor y el menú."""
    import datetime as dt
    app, deshacer = _app_portal()
    try:
        _data(monkeypatch, facturas=[
            _factura(),
            _factura(numf=11852, numf_completo="001-099-000011852", id_factura=10,
                     fecha=dt.date(2026, 8, 31), importe=-10741.46, saldo=-10741.46)])
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "AJT"
        r = c.get("/estado-de-cuenta")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "735,25" in html and "Ninguna factura vencida" in html
        assert "Próximo vencimiento: <b>01/12/2026</b>" in html
        assert "183341" in html and "al día" in html and "a su favor" in html
        assert "Edgar Ramirez" in html and "Su vendedor" in html
        assert 'class="tabbar"' in html
        # El nombre del cliente, como persona y no a los gritos.
        assert "Totoy Buitron Andres Julio" in html
    finally:
        deshacer()


def test_facturas_filtra_vencidas_y_busca_por_numero(monkeypatch):
    import datetime as dt
    app, deshacer = _app_portal()
    try:
        _data(monkeypatch, facturas=[
            _factura(),
            _factura(numf=170001, numf_completo="001-099-000170001", id_factura=2,
                     fecha=dt.date(2026, 5, 2), vencimiento=dt.date(2026, 8, 1), saldo=50)])
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "AJT"
        todo = c.get("/facturas").get_data(as_text=True)
        assert "183341" in todo and "170001" in todo and "Pendientes (2)" in todo and "Vencidas (1)" in todo
        assert "Septiembre 2026" in todo and "Mayo 2026" in todo
        vencidas = c.get("/facturas?ver=vencidas").get_data(as_text=True)
        assert "170001" in vencidas and "183341" not in vencidas
        assert "vencida hace" in vencidas
        buscada = c.get("/facturas?q=1833").get_data(as_text=True)
        assert "183341" in buscada and "170001" not in buscada
        nada = c.get("/facturas?q=999").get_data(as_text=True)
        assert "No encontramos la factura 999" in nada
    finally:
        deshacer()
