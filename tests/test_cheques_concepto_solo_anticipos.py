"""El campo Concepto de la cobranza sale SÓLO para anticipos.

TMT 2026-08-04 (tarde), dueña: *"lo del concepto opcional solo para
anticipos"*.

Nació así en el pedido de Alex —*"podemos colocar de forma manual un concepto
cuando sea anticipo"*— y se había puesto en TODOS los bloques de cheque. Una
cobranza de doce cheques quedaba con doce campos de texto libre que nadie iba a
llenar, y el cobro normal ya se explica solo: se aplica a facturas y la tirilla
del día lo dice. El concepto manual existe para lo que NO deja huella, y eso es
el anticipo.

⚠️ Ocultar un campo que tiene algo tipeado es la forma clásica de comerse un
dato en silencio — es literalmente el error del 11/06 con el N° de cheque (12
correcciones CORR + migraciones 0100-0102). Por eso acá el valor se guarda al
ocultar y se restaura al volver, igual que el N°.
"""
from __future__ import annotations

import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

RUTA = os.path.join(_REPO_ROOT, "modules", "cheques", "templates",
                    "cheques", "nuevo.html")


def _html() -> str:
    with open(RUTA, encoding="utf-8") as fh:
        return fh.read()


def _bloque_concepto(html: str) -> str:
    """El trozo de `configurarBloque` que decide el Concepto."""
    i = html.index("const conceptoInp  = bloque.querySelector('.cheque-concepto')")
    return html[i - 400 : i + 900]


# ---------------------------------------------------------------------------
# Arranca oculto y se muestra sólo con 97
# ---------------------------------------------------------------------------

def test_el_campo_nace_oculto():
    """El banco por defecto no es anticipo: si nace visible, parpadea en
    cada carga de la pantalla."""
    html = _html()
    m = re.search(r'<div class="([^"]*cheque-concepto-wrap[^"]*)"', html)
    assert m, "el Concepto perdió su wrapper — el toggle no tiene a qué agarrarse"
    assert "hidden" in m.group(1)


def test_se_muestra_solo_cuando_el_banco_es_97():
    b = _bloque_concepto(_html())
    assert "conceptoWrap.classList.toggle('hidden', !esAnticipo97)" in b, (
        "el Concepto tiene que colgar de esAnticipo97 y no del banco crudo: "
        "con 97 el medio del anticipo cambia `v` (99/90/91) y el campo "
        "desaparecería justo en el caso para el que existe"
    )


def test_el_toggle_vive_en_configurarBloque():
    """Ahí y no en un listener aparte: `configurarBloque` es lo único que
    corre para los bloques clonados con «+ Otro cheque» y para los que
    restaura el backend tras un error de validación."""
    html = _html()
    i = html.index("function configurarBloque(")
    j = html.index("\n  function ", i + 10)
    assert "cheque-concepto-wrap" in html[i:j]


# ---------------------------------------------------------------------------
# ⭐ No comerse lo tipeado
# ---------------------------------------------------------------------------

def test_al_ocultarlo_guarda_lo_que_estaba_escrito():
    b = _bloque_concepto(_html())
    assert "bloque.dataset.conceptoGuardado = conceptoInp.value" in b, (
        "sin guardar, cambiar de banco borra en silencio lo que se tipeó — "
        "el mismo error que costó 12 correcciones con el N° de cheque"
    )


def test_al_volver_a_anticipo_lo_restaura():
    b = _bloque_concepto(_html())
    assert "conceptoInp.value = bloque.dataset.conceptoGuardado" in b


def test_al_ocultarlo_limpia_el_valor_que_viajaria_en_el_POST():
    """Un concepto viejo no puede viajar en un cobro que ya no es anticipo:
    quedaría guardado en `scintela.cheque.concepto` y saldría impreso en la
    tirilla del día diciendo algo que no corresponde."""
    b = _bloque_concepto(_html())
    assert "conceptoInp.value = '';" in b


def test_no_pisa_lo_que_el_usuario_ya_tenia_escrito_al_restaurar():
    """La restauración es sólo para el campo vacío: si hay algo tipeado
    ahora, gana lo de ahora."""
    b = _bloque_concepto(_html())
    assert "!conceptoInp.value && bloque.dataset.conceptoGuardado" in b


# ---------------------------------------------------------------------------
# Que el estado se recalcule cuando el backend devuelve el form
# ---------------------------------------------------------------------------

def test_al_restaurar_los_bloques_se_reconfiguran():
    """⭐ El bloque base se configura al CARGAR la página, antes de que
    `restaurarCheques` le pise el banco.

    Sin este re-disparo, volver de un error de validación con banco 97 dejaba
    el Concepto escondido aunque el valor estuviera ahí: se ve raro y hace
    dudar de si se guardó.
    """
    html = _html()
    i = html.index("function restaurarCheques()")
    j = html.index("})();", i)
    cuerpo = html[i:j]
    assert "dispatchEvent(new Event('change'" in cuerpo


def test_el_change_sintetico_no_roba_el_foco():
    """`isTrusted` es false en un evento creado por JS, y `configurarBloque`
    sólo salta a Doc. banco cuando `fromUser` es true. Si algún día se manda
    un click real, el cursor se va del campo donde está la persona."""
    html = _html()
    assert "ev.isTrusted" in html


# ---------------------------------------------------------------------------
# Lo que NO cambia
# ---------------------------------------------------------------------------

def test_el_campo_sigue_siendo_opcional():
    """Nunca fue obligatorio y no lo es ahora: la mayoría de los anticipos
    se explican solos por `mov_doble`."""
    html = _html()
    i = html.index('name="concepto[]"')
    cell = html[i - 200 : i + 500]
    assert "required" not in cell


def test_sigue_teniendo_las_opciones_preestablecidas():
    """El datalist es la mitad del pedido de Alex: 'o tener preestablecido:
    Abono a cheques, anticipo a facturas…'."""
    html = _html()
    assert 'list="conceptos-cobro"' in html
    assert 'id="conceptos-cobro"' in html


def test_el_backend_sigue_aceptando_el_campo():
    """Esto es un cambio de PANTALLA. Si el backend dejara de leer
    `concepto[]`, los anticipos ya cargados perderían el suyo al editarse."""
    from modules.cheques import views

    import inspect

    assert 'concepto[]' in inspect.getsource(views)
