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

    de_la_oficina = re.search(r"<style>(.*?)</style>",
                              _sin_comentarios(lote), re.S).group(1)
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
    """Un saldo negativo con un signo menos adelante lo lee mal cualquiera. Y
    un cliente que ve '-$ 500' de deuda llama preguntando qué pasó."""
    assert "a-favor" in PANTALLA
    assert "saldo a favor" in PANTALLA.lower()


def test_la_pantalla_compara_vencimientos_contra_una_FECHA():
    """🚨 `factura.vencimiento` viene como `date` de la base. Comparar un date
    con un string no da False: LEVANTA, y se cae la pantalla entera."""
    assert '"hoy_iso": date.today(),' in VISTAS
    assert "hoy_iso" in PANTALLA


def test_los_numeros_van_en_formato_de_ecuador():
    """🔢 Punto de miles, coma de decimales: `2.812,86`, no `2812.86`.

    Los filtros de la casa (`money_es`, `fecha_es`) se registran en
    `create_app` ANTES de elegir el modo, así que el portal los tiene. Formatear
    a mano con `'%.2f'|format` sale en formato yanqui — y salió, en la primera
    corrida contra producción."""
    assert "'%.2f'|format" not in PANTALLA, (
        "hay un número formateado a mano: va a salir en formato yanqui")
    assert "money_es" in PANTALLA


def test_las_fechas_van_por_el_filtro_de_la_casa():
    """`strftime` se cae con un `None` y con un texto ISO. `fecha_es` aguanta
    los tres casos, que es justamente por lo que existe."""
    assert "strftime" not in PANTALLA
    assert "fecha_es" in PANTALLA
