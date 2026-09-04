"""El estado de cuenta que ve el cliente en el portal.

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
PANTALLA = (TPL / "estado_cuenta.html").read_text(encoding="utf-8")
HOJA = (TPL / "estado_cuenta_impreso.html").read_text(encoding="utf-8")
VISTAS = (ROOT / "modules" / "portal" / "views.py").read_text(encoding="utf-8")


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


def test_la_pantalla_avisa_lo_vencido():
    """Es el único dato de la pantalla que le pide algo al cliente."""
    assert "saldo_vencido" in PANTALLA
    assert "n_vencidas" in PANTALLA


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
    assert "strftime" not in PANTALLA
    assert "fecha_es" in MOVIMIENTOS


BASE_PORTAL = (TPL / "base.html").read_text(encoding="utf-8")


def test_el_portal_y_mi_cartera_comparten_los_estilos():
    """⭐ Dueña: *"no hace falta que rediseñes, que sea consistente con lo que
    ya hay en Programa Core"*.

    El cliente y el vendedor miran el mismo producto desde el mismo teléfono.
    Los estilos salen de UN archivo (`mi_cartera/_estilos.html`) que incluyen
    los dos, no de dos hojas parecidas que se separan a la primera corrección.
    """
    assert '{% include "mi_cartera/_estilos.html" %}' in BASE_PORTAL
    base_vend = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
                 / "base.html").read_text(encoding="utf-8")
    assert '{% include "mi_cartera/_estilos.html" %}' in base_vend


def test_el_portal_usa_las_variables_de_la_casa_y_no_colores_sueltos():
    """Si el portal escribiera `#e30613` a mano, cambiar el color de la marca
    en un lado no lo cambiaría en el otro."""
    import re
    for pantalla in (BASE_PORTAL, PANTALLA):
        estilos = "\n".join(re.findall(r"<style>(.*?)</style>", pantalla, re.S))
        sueltos = [c for c in re.findall(r"#[0-9a-fA-F]{6}\b", estilos)]
        assert not sueltos, f"colores escritos a mano: {sueltos}"
    assert "var(--accent)" in BASE_PORTAL


def test_la_pantalla_de_la_cuenta_usa_el_hero_del_vendedor():
    """El mismo bloque de arriba, con los mismos rótulos: si los dos están
    mirando lo mismo, tienen que verlo igual."""
    assert 'class="hero"' in PANTALLA
    assert 'class="lbl"' in PANTALLA and 'class="val"' in PANTALLA
    assert 'class="split"' in PANTALLA


def test_el_cliente_ve_EL_MISMO_cuerpo_que_su_vendedor():
    """⭐ Dueña, 24/08: *"que sea más parecido a como ve el cliente el
    vendedor"*. No parecido: **el mismo**. Las pestañas, la tabla y los
    rótulos salen del parcial compartido, que también incluye la ficha del
    vendedor.

    Esa tabla llevó varias vueltas de ajuste para entrar en 390 px —la columna
    que no aporta no se dibuja, la fecha va dd/mm/AA, el acumulado corre de
    arriba hacia abajo—. Nada de eso se rehace por segunda vez."""
    assert '{% include "mi_cartera/_movimientos.html" %}' in PANTALLA
    ficha = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
             / "cliente.html").read_text(encoding="utf-8")
    assert '{% include "mi_cartera/_movimientos.html" %}' in ficha


def test_el_parcial_tiene_lo_que_hace_falta_para_dibujarlo():
    """Lo que el parcial espera en el contexto. Si mañana pide algo más y el
    portal no se lo pasa, la pantalla del cliente se cae y la del vendedor no
    — que es la forma más fácil de romper esto sin enterarse."""
    for nombre in ("facturas", "cheques", "saldo_neto", "qv"):
        assert f"{{% set {nombre} =" in PANTALLA or f"set {nombre} =" in PANTALLA, nombre


def test_el_boton_de_salir_no_es_un_caracter():
    """🚨 Regla ya pagada dos veces en este portal (el emoji de WhatsApp el
    20/08, el ⎙ de imprimir el 24/08): en un botón no va un carácter especial,
    va un SVG. En Android sale el cuadradito del glifo que falta."""
    assert "--ico-salir" in PANTALLA
    assert "svg+xml" in PANTALLA
    import re
    boton = re.search(r'<a class="iconbtn salir".*?</a>', PANTALLA, re.S).group(0)
    assert not re.search(r">[^<\s]", boton), "el botón tiene texto adentro; va vacío con máscara"


def test_en_pantalla_grande_se_ensancha():
    """⭐ Dueña: *"también esto puede funcionar en web y en mobile"*. Los
    estilos compartidos están afinados para 390 px —la tabla baja a 11 px para
    que entren siete columnas— y en un monitor eso se lee diminuto.

    Ensancha SÓLO el portal del cliente: la ficha del vendedor se mira siempre
    desde el celular, y tocarle el ancho sería cambiarle la pantalla a alguien
    que no lo pidió."""
    assert "@media (min-width: 700px)" in PANTALLA
    ficha = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
             / "cliente.html").read_text(encoding="utf-8")
    assert "min-width: 700px" not in ficha


# ---------------------------------------------------------------------------
# El PDF
# ---------------------------------------------------------------------------

HOJA_PORTAL = (TPL / "estado_cuenta_impreso.html").read_text(encoding="utf-8")


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


def test_hay_UN_solo_boton_para_el_estado_de_cuenta():
    """⭐ Dueña, 24/08: *"ver hoja para imprimir y descargar archivo es lo
    mismo"*. Y tenía razón: los dos llevan al mismo documento, uno en pantalla
    y el otro en archivo. Dos botones para lo mismo obligan a elegir entre dos
    cosas que no se distinguen.

    Queda el archivo, y la hoja en pantalla aparece EN SU LUGAR cuando el
    servidor no puede generarlo: la acción no desaparece, cambia de forma."""
    assert PANTALLA.count('href="/estado-de-cuenta.pdf"') == 1
    assert PANTALLA.count('href="/estado-de-cuenta/imprimir"') == 1
    assert "{% else %}" in PANTALLA[PANTALLA.index("pdf_disponible()"):
                                    PANTALLA.index("/estado-de-cuenta/imprimir")]


def test_ya_no_se_esconde_ninguna_columna_en_el_celular():
    """🚨 Dueña 27/08/2026: *"todo tiene que entrar y no salirse así de feo"*.

    El parche del 24/08 escondía la columna ACUM. en pantallas angostas porque
    seis columnas no entraban. Desde el 27/08 el acumulado corre en un <small>
    bajo el saldo (parcial compartido `_movimientos.html`) y la tabla entra
    entera hasta en un celular de 360 px. Si el parche hubiera quedado, ahora
    escondería la columna del SALDO — el dato.

    La red de seguridad sí queda: si un caso extremo no entra, la tabla se
    desliza adentro de su tarjeta en vez de salirse por la derecha."""
    assert "last-child{display:none}" not in PANTALLA
    assert ".card:has(> .ec)" in PANTALLA


def test_el_acumulado_apilado_es_el_MISMO_para_cliente_y_vendedor():
    """Dueña 24/08: *"que sea más parecido a como ve el cliente el vendedor"*
    — no parecido: el mismo. El apilado vive en el parcial compartido, así que
    las dos pantallas lo dibujan igual; este test impide que a una de las dos
    le vuelva una columna propia."""
    compartido = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
                  / "_movimientos.html").read_text(encoding="utf-8")
    # El rótulo va apilado: "Saldo" y, en un <small> debajo, "· acum.".
    assert "Saldo <small>· acum.</small>" in compartido
    ficha = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
             / "cliente.html").read_text(encoding="utf-8")
    assert "max-width: 560px" not in ficha


def test_el_menu_esta_en_todas_las_pantallas_y_los_botones_no_se_repiten():
    """🐞 04/09/2026, probando con AJT: "Ver mis despachos" estaba DOS veces,
    y el botón de descargar salía como texto pelado porque su clase
    (`.btn-pr`) ya no existía en los estilos compartidos. Dueña: *"que sea
    web, los botones hacen que la info quede muy mal"*."""
    t = (ROOT / "modules" / "portal" / "templates" / "portal"
         / "estado_cuenta.html").read_text(encoding="utf-8")
    # Pagos y despachos viven en el menú de abajo, no en botones sueltos.
    assert 'href="/despachos"' not in t and 'href="/mis-pagos"' not in t
    assert 'class="acciones"' in t and ".acciones a{" in t
    menu = (ROOT / "modules" / "portal" / "templates" / "portal"
            / "_menu.html").read_text(encoding="utf-8")
    assert 'href="/mis-pagos"' in menu and 'href="/despachos"' in menu
    for pantalla in ("estado_cuenta", "pagos", "despachos", "despacho", "factura"):
        cuerpo = (ROOT / "modules" / "portal" / "templates" / "portal"
                  / f"{pantalla}.html").read_text(encoding="utf-8")
        assert '{% include "portal/_menu.html" %}' in cuerpo, pantalla
    assert "btn-pr" not in t.split("<style>")[0].replace("`.btn-pr`", "")
    base = (ROOT / "modules" / "portal" / "templates" / "portal"
            / "base.html").read_text(encoding="utf-8")
    assert "@media (min-width:900px)" in base


def test_la_pestana_cheques_abre_los_cheques(monkeypatch):
    """🐞 04/09/2026, con AJT: el link "Cheques" no hacía nada. El parcial
    compartido decide por `tab`, y el portal no se lo pasaba: siempre
    Facturas. Se prueba por la PANTALLA, con ?tab=cheques."""
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    try:
        from modules.informes import queries as q
        monkeypatch.setattr(q, "estado_cuenta_cliente", lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "ALMACENES TEXTILES", "ruc": "1791234567001"},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": q.totales_estado_cuenta_en_cero(),
        })
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "ATE"
        html = c.get("/estado-de-cuenta?tab=cheques").get_data(as_text=True)
        assert '?tab=cheques" class="on"' in html
        assert '?tab=facturas" class="on"' not in html
        html = c.get("/estado-de-cuenta").get_data(as_text=True)
        assert '?tab=facturas" class="on"' in html
    finally:
        deshacer()


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
