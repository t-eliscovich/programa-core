"""La frase del renglón de stock respeta el sentido del flujo (TMT 2026-08-11).

Dueña, sobre un renglón que decía "hil. y term. → tej.": *"este renglón no
tiene sentido"*. El flujo va hilado → tejido → terminado; la tela terminada no
vuelve a ser cruda. Y lo caro no era la frase rara sino que tapaba la noticia:
esos 1.706 kg de terminado salieron en un despacho a cliente, $ 8.947.
"""
from modules.informes.foto import _texto_stock


def _est(hil=0.0, tej=0.0, term=0.0):
    return {"hilado": {"dkg": hil}, "tejido": {"dkg": tej},
            "terminado": {"dkg": term}}


def test_el_caso_que_no_tenia_sentido():
    """hilado −45, tejido +26, terminado −1.706 (11/08 10:10)."""
    assert _texto_stock(_est(hil=-45.5, tej=26.4, term=-1706.3)) == (
        "hil. → tej. · salió de term.")


def test_una_baja_no_se_explica_con_una_suba_RIO_ARRIBA():
    """Vender terminado y recibir hilado en la misma ventana son DOS hechos.
    Antes salía "term. → hil.", que es el mismo sinsentido al revés."""
    assert _texto_stock(_est(hil=22106.4, term=-1706.3)) == (
        "salió de term. · entró a hil.")


# ── lo que ya andaba tiene que seguir igual ────────────────────────────────

def test_el_traspaso_normal_no_cambia():
    assert _texto_stock(_est(hil=-350.4, tej=295.9)) == "hil. → tej."
    assert _texto_stock(_est(tej=-68.2, term=65.4)) == "tej. → term."


def test_solo_entro_o_solo_salio():
    assert _texto_stock(_est(term=102.3)) == "entró a term."
    assert _texto_stock(_est(hil=-4948.0)) == "salió de hil."


def test_la_cadena_entera_en_una_ventana():
    """hilado baja, tejido baja, terminado sube: los dos de arriba tienen
    destino río abajo, así que es un traspaso de punta a punta."""
    assert _texto_stock(_est(hil=-300.0, tej=-50.0, term=340.0)) == (
        "hil. y tej. → term.")


def test_sin_movimiento_apreciable():
    assert _texto_stock(_est(hil=0.5, tej=-0.9)) == "movimiento de stock"
    assert _texto_stock({}) == "movimiento de stock"
