"""Las notas de crédito de verdad se autotildan aunque el cheque no llegue.

Dueña 17/08/2026, trayendo lo que le decía Alex: *"las notas de crédito a veces
no las lee muy bien, selecciona una factura y deselecciona y hace cosas hasta
que tome el negativo"*.

Medido sobre BED ese día: sus 4 notas de crédito reales (12 y 14/08) caían en
las posiciones **157, 163, 164 y 165 de 169**, porque el listado va por fecha y
las NC son lo más nuevo del cliente. El recorrido FIFO se corta cuando se acaba
la plata del cheque: con $12.000 llegaba hasta el 22/05, o sea la posición 9.
Conclusión: **por el recorrido no se tildaban nunca** y había que ir a mano.

El arreglo las absorbe ANTES del FIFO. No le sacan lugar a ninguna factura: una
NC no consume plata, la devuelve — agranda el pool y el neto sigue dando
exactamente el importe del cheque.

## Lo que este test protege de que se rompa otra vez

1. **Sólo `importe < 0`.** Una factura de importe POSITIVO con saldo negativo
   NO es una NC: es una sobre-aplicación tipeada a mano (regla del 06/07, ver
   `views.py`). BED tenía 12 de esas. Meterlas en la misma bolsa llevaba la
   pantalla de 1 fila tildada a 19 con un cheque de $200, y la dueña lo
   descartó explícitamente el 17/08.
2. **Sin cheques cargados no se tilda nada** — elegir el cliente no puede
   dejar la pantalla con filas marcadas solas.
3. **Las editadas a mano quedan afuera** (`userValues` manda): si destilda una
   NC, se respeta.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def _bloque_nc() -> str:
    """El loop que absorbe las NC, entre el cálculo del pool y el FIFO."""
    i = NUEVO.index("if (chequeImps.length) {")
    j = NUEVO.index("// 3) FIFO POR FECHA con el pool", i)
    return NUEVO[i:j]


def test_las_nc_se_absorben_antes_del_fifo():
    """El bloque tiene que estar ANTES del `for` del FIFO, no adentro.

    Si quedara adentro volvería a depender de que el pool alcance, que es
    exactamente el bug.
    """
    pos_nc = NUEVO.index("if (chequeImps.length) {")
    pos_fifo = NUEVO.index("// 3) FIFO POR FECHA con el pool")
    pos_pool = NUEVO.index("let pool = libres.reduce")
    assert pos_pool < pos_nc < pos_fifo


def test_solo_las_de_importe_negativo():
    """La sobre-aplicación tipeada (importe > 0, saldo < 0) NO entra acá."""
    b = _bloque_nc()
    assert "parseFloat(f.importe) || 0) >= 0) continue" in b, (
        "el filtro tiene que ser por IMPORTE negativo, no por saldo negativo"
    )
    assert "s >= -0.005) continue" in b, "y además tiene que tener crédito vivo"


def test_la_nc_agranda_el_pool():
    b = _bloque_nc()
    assert "pool -= s;" in b, "restar un negativo AGRANDA el pool"


def test_no_se_tilda_nada_sin_cheques_cargados():
    assert "if (chequeImps.length) {" in NUEVO


def test_respeta_lo_editado_a_mano():
    """Recorre `porFecha`, que ya excluye las filas con `userValues`."""
    b = _bloque_nc()
    assert "for (const f of porFecha)" in b
    assert "if (asignadas.has(f.id_factura)) continue;" in b
    # y `porFecha` se construye filtrando las editadas
    assert "const porFecha = facturas.filter(f => Math.abs(parseFloat(f.saldo) || 0) > 0.005 && !_editada(f));" in NUEVO


def test_el_anticipo_y_ninguna_siguen_apagando_la_sugerencia():
    """El bloque vive dentro del `if (!_hayAnticipo && !sinAplicar)`."""
    pos_guard = NUEVO.index("if (!_hayAnticipo && !sinAplicar) {")
    pos_nc = NUEVO.index("if (chequeImps.length) {")
    pos_cierre = NUEVO.index("// TMT 2026-05-19 — item 4: running total", pos_nc)
    assert pos_guard < pos_nc < pos_cierre
