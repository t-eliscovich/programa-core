"""En la proforma, ENTER sobre el Cliente entra DERECHO a la primera tela.

Tamara 2026-08-24: *"que con enter se pase directo a la tela y una sola fila.
O sea pongo BED, no hace falta ir a la fecha y que ya pueda poner tela"*.

Dos cosas quedaban en el medio del que carga rápido:

1. El ENTER del código de cliente caía en **Fecha**, que ya viene puesta con
   el día de hoy y casi nunca se toca. Un tab de más en cada cotización.
2. El formulario arrancaba con **dos** filas y la segunda quedaba vacía casi
   siempre. El ENTER del Precio ya agrega la fila que haga falta.

El grueso del ENTER vive en JS, así que se verifica sobre la plantilla (mismo
criterio que test_proformas_iva.py).
"""
from __future__ import annotations

from pathlib import Path

_NUEVA = "modules/proformas/templates/proformas/nueva.html"


def _plantilla() -> str:
    return Path(_NUEVA).read_text(encoding="utf-8")


def test_arranca_con_una_sola_fila():
    t = _plantilla()
    # El arranque en blanco llama UNA vez a agregarFila().
    arranque = t.split("if (GUARDADAS.length)")[1].split("recalc();")[0]
    assert arranque.count("agregarFila()") == 2  # una al reabrir, una en blanco
    assert "agregarFila(); agregarFila();" not in t


def test_enter_en_el_cliente_va_a_la_primera_tela():
    t = _plantilla()
    assert "function irAPrimeraTela()" in t
    assert "document.querySelector('#lineas-body .js-tipo')" in t
    bloque = t.split("codigoInput.addEventListener('keydown'")[1].split("});")[0]
    # Con el listado de clientes abierto: elige y sigue a la tela.
    assert "ddElegir(ddItems[ddIdx >= 0 ? ddIdx : 0]); irAPrimeraTela();" in bloque
    # Con el listado cerrado (código tipeado entero): también sigue a la tela,
    # sin dejar de traer el descuento del cliente.
    cerrado = bloque.split("if (clientesDD.classList.contains('hidden'))")[1]
    assert "__profPrefillCliente" in cerrado
    assert "irAPrimeraTela();" in cerrado


def test_la_fecha_sigue_editable_y_nadie_le_manda_el_foco():
    """La Fecha no se saca ni se bloquea: lo que cambia es que el ENTER no
    pasa por ahí. Si alguien volviera a enfocarla desde el JS, el atajo se
    perdería de nuevo sin que se vea en pantalla."""
    t = _plantilla()
    cabecera = t.split('name="fecha"')[1].split("</label>")[0]
    assert "readonly" not in cabecera and "disabled" not in cabecera
    assert "fechaAutoFormat" in cabecera  # se sigue pudiendo tipear
    # Ningún .focus() del JS apunta a la Fecha.
    for linea in t.splitlines():
        if ".focus()" in linea:
            assert "fecha" not in linea.lower()
