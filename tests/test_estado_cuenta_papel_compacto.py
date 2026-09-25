"""TMT 2026-09-25 (dueña): "la impresión de estado de cuenta tiene mucho
espacio en blanco y consume mucho papel" + "ocupa mucho espacio las
totalizaciones, que muestre la última y una flechita para ver anteriores"."""
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "modules/informes/templates/informes"


def test_cliente_sin_cheques_por_cobrar_no_imprime_el_bloque_de_cheques():
    src = (TPL / "_estado_cuenta_impreso.html").read_text("utf-8")
    assert "ec-ch-vacio" in src
    assert "body:not(.ec-print-cheques) main .ec-ch-vacio { display: none !important; }" in src


def test_el_titulo_facturas_no_va_al_papel():
    src = (TPL / "_estado_cuenta_impreso.html").read_text("utf-8")
    assert "ec-tit-facturas" in src
    assert "main .ec-tit-facturas { display: none !important; }" in src


def test_totalizaciones_muestra_la_ultima_y_pliega_las_anteriores():
    src = (TPL / "estado_cuenta.html").read_text("utf-8")
    assert "{{ _tz_fila(totalizares[0]) }}" in src
    assert "{% for tz in totalizares[1:] %}" in src
    assert 'class="ec-tz-anteriores"' in src
    assert "{% for tz in totalizares %}" not in src


def test_sin_cheques_el_papel_lo_dice_en_un_renglon():
    src = (TPL / "_estado_cuenta_impreso.html").read_text("utf-8")
    assert "Cheques: sin cheques en cartera." in src
    assert ".ec-ch-vacio-linea { display: none; }" in src
