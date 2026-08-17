"""El buscador de facturas esconde filas; no las saca del formulario.

Hallazgo de la auditoría del 17/08/2026, y es el más caro de los del día
porque llegaba hasta lo que se GUARDA.

`render(filtro)` filtraba las facturas ANTES de dibujarlas, así que el
`<input name="aplicar[...]">` de las que no coincidían dejaba de existir en el
DOM — o sea, en el `<form>`. Y como `updateSummary()` y el handler de submit
recorren el `tbody`, lo que no estaba renderizado no existía para ninguno de
los dos.

Reproducido sobre AJ2, cheque de $3.000 repartido en 13 facturas, escribiendo
"175020" en el buscador:

    el pie mostraba  -> Aplicado 1.330,61 · Restante 1.669,39   (era 3.000 / 0)
    el submit mandaba ->  1 aplicación de 1.330,61, faltaban 12 facturas

En ese caso el backend lo frena, porque la diferencia supera los $50. Pero el
freno depende del monto: si lo que queda afuera es menos de $50, o si se tilda
"Aprobar diferencia", pasa y se guarda mal. Y aunque frene, el usuario recibe
un "faltan $1.669,39" justo después de haber dejado la pantalla cuadrada.

Arreglo: dibujar SIEMPRE todas las filas y ponerle `display:none` a las que no
coinciden.

## Los bordes que este test cuida

* El cartel "Sin facturas que coincidan" ya no puede depender de que `rowsHtml`
  quede vacío —ahora nunca lo está—, así que se decide por un contador de
  visibles.
* El cursor de las flechas ↑↓ tiene que saltear las filas ocultas: no puede
  pararse sobre algo que no se ve.
* `display:none` y no `hidden`: cualquier `display:` del CSS le gana al
  atributo (ya nos pasó).
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def test_no_se_filtra_la_lista_antes_de_dibujar():
    """El `.filter(...)` sobre `facturas` era el bug. No puede volver."""
    assert "const rowsHtml = facturas\n      .map(f => {" in NUEVO
    assert "const _coincide = (f) => {" in NUEVO


def test_la_fila_que_no_coincide_se_oculta():
    assert "const _oculta = !_coincide(f);" in NUEVO
    assert """<tr class="border-t ${rowClass}"${_oculta ? ' style="display:none"' : ''}>""" in NUEVO


def test_usa_display_none_y_no_el_atributo_hidden():
    """`[hidden]` lo pisa cualquier regla `display:` — ya nos mordió antes."""
    i = NUEVO.index("const _oculta = !_coincide(f);")
    j = NUEVO.index("</tr>", i)
    tramo = NUEVO[i:j]
    assert "style=\"display:none\"" in tramo
    assert " hidden>" not in tramo


def test_el_cartel_de_sin_resultados_va_por_el_contador():
    assert "let _visibles = 0;" in NUEVO
    assert "if (!_oculta) _visibles++;" in NUEVO
    assert "q && _visibles === 0" in NUEVO


def test_el_cursor_de_teclado_saltea_las_ocultas():
    i = NUEVO.index("function filasFact()")
    j = NUEVO.index("}", NUEVO.index("return [...tbody", i))
    cuerpo = NUEVO[i:j]
    assert "tr.style.display !== 'none'" in cuerpo


def test_el_input_de_aplicar_sigue_dentro_del_form():
    """Es lo que se manda al backend: si sale del DOM, se pierde la plata."""
    assert 'name="aplicar[${f.id_factura}]"' in NUEVO
