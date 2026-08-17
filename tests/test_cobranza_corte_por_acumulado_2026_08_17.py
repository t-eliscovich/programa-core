"""El cheque que cae justo en el ACUM. SALDO corta ahí, sin partir facturas.

Dueña 17/08/2026, con el caso de Alex sobre AJ2:

    "Alex pone ese monto porque quiere que se cubra FIFO hasta la factura 785
     que tiene acumulado exacto ese monto. Se tilda cualquier cosa."

La columna **ACUM. SALDO** es el total corrido de saldos en el orden de la
pantalla. Alex la usa al revés de como se lee: busca la fila cuyo acumulado da
un número redondo de cobrar, y ESE es el importe que le pide al cliente.

El detalle que rompía la promesa: el acumulado **sube con las facturas y BAJA
con las notas de crédito**, así que el mismo número puede aparecer en varios
puntos de la lista. El FIFO corta en cuanto se le acaba la plata —el primer
punto que lo alcanza— y ahí casi siempre parte una factura al medio.

Medido sobre AJ2 con $4.098,68 (152 facturas abiertas):

    hoy   -> 13 filas, corta en la fila 13, parcial de 1.349,64
             sobre una factura de 1.381,06
    corte -> 23 filas, corta en la fila 23 (la 10785), CERO parciales

Los dos aplican exactamente 4.098,68. La diferencia es que uno deja una
factura a medias y el otro no.

## Los dos frenos que este test protege

`exactSet.size === 0` y `fixedNet == 0`: el corte sólo corre cuando el pool
**es** el importe del cheque tal cual. Si ya hubo match exacto por saldo, o si
el usuario tipeó algún monto, lo que se va a aplicar dejó de ser lo que la
columna describe — y un corte calculado sobre un acumulado que ya no aplica
tildaría cualquier cosa. Verificado: AJ2 con $1.381,06 (el saldo exacto de una
factura) sigue haciendo el match de siempre sobre esa sola fila.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def _bloque() -> str:
    i = NUEVO.index("let cortePorAcum = -1;")
    j = NUEVO.index("// 3) FIFO POR FECHA con el pool", i)
    return NUEVO[i:j]


def test_el_acumulado_se_calcula_una_sola_vez_y_antes_del_reparto():
    """UNA definición: la que se pinta y la que decide el corte son la misma.

    Si el corte usara su propia cuenta, podría caer en una fila distinta de la
    que el usuario está leyendo en la columna — el peor de los mundos.
    """
    assert NUEVO.count("const acumPorFactura = {};") == 1
    pos_acum = NUEVO.index("const acumPorFactura = {};")
    pos_reparto = NUEVO.index("if (!_hayAnticipo && !sinAplicar) {")
    pos_uso = NUEVO.index("let cortePorAcum = -1;")
    pos_columna = NUEVO.index("fmt(acumPorFactura[f.id_factura] || 0)")
    assert pos_acum < pos_reparto < pos_uso < pos_columna


def test_corta_en_el_primer_acumulado_que_da_exacto():
    b = _bloque()
    assert "Math.abs(_a - pool) < 0.005" in b
    assert "break;" in b, "el PRIMER corte, no el último"


def test_las_filas_del_corte_van_enteras():
    """El punto del corte es justamente no dejar parciales."""
    b = _bloque()
    assert "distribucion[f.id_factura] = s;" in b
    assert "Math.min" not in b, "un corte exacto nunca recorta una fila"


def test_no_corre_si_hubo_match_exacto_o_edicion_a_mano():
    b = _bloque()
    assert "exactSet.size === 0" in b
    assert "Math.abs(fixedNet) < 0.005" in b


def test_no_corre_sin_cheques_cargados():
    b = _bloque()
    assert "chequeImps.length" in b
    assert "pool > 0.005" in b


def test_el_fifo_de_siempre_sigue_abajo():
    """Sin corte exacto, no cambia nada: el FIFO queda intacto."""
    i = NUEVO.index("// 3) FIFO POR FECHA con el pool")
    fifo = NUEVO[i:i + 900]
    assert "for (const f of porFecha) {" in fifo
    assert "if (pool <= 0.005) break;" in fifo
    assert "const ap = s < 0 ? s : Math.min(s, pool);" in fifo
