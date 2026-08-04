"""Anular un cobro en efectivo que ADOPTÓ una fila de caja: desenlazar, no compensar.

TMT 2026-08-04. Al convertir los 5 cobros en efectivo sueltos, 3 salieron mal
(PC ya tenía ese cobro aplicado por otro camino) y hubo que sacarlos. Ahí
apareció que «anular por error de carga» **compensa la caja con una salida** —
correcto cuando el cheque CREÓ esa fila, falso cuando la ADOPTÓ: la plata había
entrado a la caja antes y por su cuenta, así que compensarla dejaba la caja
corta por un cobro que sí ocurrió ($3.002,86 en el caso real).

El hecho que distingue los dos casos: una fila creada por el cheque nace en la
MISMA transacción, así que su `fecha_crea` es la del cheque. Una adoptada es más
vieja. Se decide por ese hecho y no por un marcador, para que valga también
sobre los cheques que ya existen.
"""
from __future__ import annotations

import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as q  # noqa: E402


def _bloque_c() -> str:
    """El brazo `stat_prev == 'C'` de anular_por_error_de_carga."""
    src = inspect.getsource(q.anular_por_error_de_carga)
    i = src.index('elif stat_prev == "C"')
    return src[i : i + 3200]


def test_distingue_la_caja_adoptada_de_la_creada():
    b = _bloque_c()
    assert "cj.fecha_crea < c.fecha_crea" in b, (
        "sin comparar fecha_crea no puede saber si la adoptó o la creó"
    )


def test_la_adoptada_se_desenlaza_y_no_se_compensa():
    b = _bloque_c()
    i_if = b.index("if adoptada:")
    i_else = b.index("else:", i_if)
    rama = b[i_if:i_else]
    assert "SET id_cheque = NULL" in rama, "tiene que soltar la fila, no borrarla"
    assert "insert_movimiento_caja" not in rama, (
        "compensar una caja adoptada la deja corta por plata que sí entró"
    )


def test_la_creada_sigue_compensando_como_siempre():
    b = _bloque_c()
    i_else = b.index("else:", b.index("if adoptada:"))
    assert "insert_movimiento_caja" in b[i_else:]
    assert 'tipo="E" if importe < 0 else "S"' in b[i_else:], (
        "regresión TMT 2026-07-30: el efectivo negativo se anula con una E"
    )


def test_el_desenlace_no_le_roba_la_caja_a_otro_cheque():
    b = _bloque_c()
    upd = b[b.index("SET id_cheque = NULL") : b.index("compensacion = {")]
    assert "id_caja = %s" in upd and "id_cheque = %s" in upd, (
        "el WHERE tiene que atar las dos puntas"
    )


def test_el_resultado_dice_cual_de_los_dos_caminos_tomo():
    """Quien mira el historial tiene que poder distinguirlos."""
    b = _bloque_c()
    assert "caja_desenlazada" in b
    assert '{"tipo": "caja", "id": res["id_caja"]}' in b


def test_la_consulta_corre_en_la_misma_transaccion():
    """Fuera de la tx leería un estado que el rollback después descarta."""
    b = _bloque_c()
    cons = b[b.index("adoptada = db.fetch_one") : b.index("if adoptada:")]
    assert "conn=conn" in cons
    upd = b[b.index("SET id_cheque = NULL") : b.index("compensacion = {")]
    assert "conn=conn" in upd


def test_el_mov_doble_del_link_se_reversa_igual():
    """Si queda 'activo', el cheque parece seguir atado a una caja que soltó."""
    src = inspect.getsource(q.anular_por_error_de_carga)
    assert "cheque_efectivo_to_caja" in src
    assert "estado='reversado'" in src
