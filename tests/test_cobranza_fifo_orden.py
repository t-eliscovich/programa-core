"""El orden FIFO de la cobranza está CLAVADO. No cambiarlo a DESC.

POR QUÉ EXISTE ESTE TEST (TMT 2026-07-30)
-----------------------------------------
El 30/07 se descubrió que el FoxPro imputa al REVÉS que PC. `ALTAS.PRG`,
`PROCEDURE REPLA`:

    GO BOTT
    REPLA ABONO WITH ABONO+D->IMPORTE, SALDO WITH SALDO-D->IMPORTE, ...

`GO BOTT` va a la ÚLTIMA fila del cliente, que con el `SORT ON FECHA, NUMF`
del pase diario es la MÁS NUEVA — y sin ningún `IF`, así que el saldo se va a
negativo. PC hace FIFO: la más vieja primero.

Se probó alinear PC al dBase (`ORDER BY fecha DESC`) y **la dueña lo revirtió
en el acto**: *"estamos cambiando algo de la cobranza que ya teníamos hecho?
eso no me gusta. mejor no."*

El riesgo que cubre este test es concreto: alguien lee la nota de ALTAS.PRG en
el código, cree que PC tiene un bug de paridad y "arregla" el ORDER BY a DESC.
Eso cambiaría a qué factura se imputa CADA cobranza futura, en silencio, sin
que ningún otro test se caiga. Si este test se pone rojo, la respuesta correcta
es revertir el ORDER BY, no actualizar el test.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as q  # noqa: E402

# El orden canónico, textual. Si cambia, es una decisión de negocio, no un fix.
ORDEN_FIFO = "ORDER BY fecha, vencimiento NULLS LAST, numf"


class _CapturaSQL:
    """Captura el SQL sin tocar la DB (fetch_all devuelve vacío)."""

    def __init__(self):
        self.sqls: list[str] = []

    def fetch_all(self, sql, params=None, conn=None):
        self.sqls.append(sql)
        return []


def _sql_de_facturas_pendientes(monkeypatch) -> str:
    cap = _CapturaSQL()
    monkeypatch.setattr(q, "db", cap, raising=True)
    q.facturas_pendientes("AJO")
    assert len(cap.sqls) == 1, "facturas_pendientes debería hacer UNA query"
    return cap.sqls[0]


def test_facturas_pendientes_ordena_fifo(monkeypatch):
    """La más VIEJA primero: fecha ASC, vencimiento ASC, numf ASC."""
    sql = _sql_de_facturas_pendientes(monkeypatch)
    assert ORDEN_FIFO in sql, (
        "El ORDER BY de facturas_pendientes cambió. La cobranza de PC imputa "
        "FIFO (la más vieja primero) por decisión de la dueña del 30/07/2026. "
        "Que el dBase impute al revés (ALTAS.PRG / GO BOTT) NO es razón para "
        "cambiarlo."
    )


def test_facturas_pendientes_no_es_desc(monkeypatch):
    """Ningún DESC en el orden — ese fue el cambio probado y revertido."""
    sql = _sql_de_facturas_pendientes(monkeypatch)
    orden = sql[sql.index("ORDER BY"):]
    assert "DESC" not in orden.upper(), (
        "Apareció un DESC en el ORDER BY de facturas_pendientes. Eso imputa "
        "como el FoxPro (la factura más NUEVA, ALTAS.PRG PROCEDURE REPLA) y ya "
        "se probó y se revirtió el 30/07/2026 a pedido de la dueña."
    )


def test_orden_fifo_documentado_en_el_codigo():
    """La nota que explica por qué no se toca sigue al lado del ORDER BY.

    Si alguien borra la explicación, el próximo que lea el código no tiene cómo
    saber que la divergencia con el dBase es deliberada.
    """
    ruta = os.path.join(_REPO_ROOT, "modules", "cheques", "queries.py")
    with open(ruta, encoding="utf-8") as fh:
        src = fh.read()
    assert "NO CAMBIAR ESTE ORDEN" in src
    assert "GO BOTT" in src, "la referencia a ALTAS.PRG explica la divergencia"
