"""El banco cobra una comisión por protestar un cheque, y va al libro.

dBase `MODIFICA.PRG` L314-318: cuando emite la ND del cheque devuelto, mete
ADEMÁS un segundo renglón `GS. cheq. <cliente>` por `GCR` = **2 en Pichincha /
5 en el resto**. Programa Core emitía sólo la ND, así que la comisión aparecía
en el extracto del banco y no en el libro: **una diferencia de conciliación en
cada protesto**. TMT 2026-08-11 (dueña: "hacelo, no es un monto grande").

Un cheque protestado llega a devuelto por DOS caminos —el asistente de rebote
('9') y el cambio plano a 1/2/3— y los dos son el mismo hecho. Lo que se prueba
es que ninguno de los dos se olvide, no uno en particular.
"""
from __future__ import annotations

import inspect
import re

import pytest

from modules.cheques import queries

# ---------------------------------------------------------------------------
# El monto sale del NOMBRE del banco, no del número
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "nombre,esperado",
    [
        ("PICHINCHA", 2.0),
        ("BANCO PICHINCHA", 2.0),
        ("pichinc", 2.0),
        ("INTERNACIONAL", 5.0),
        ("PRODUBANCO", 5.0),
        ("", 5.0),
        (None, 5.0),
    ],
)
def test_cuanto_cobra_cada_banco(nombre, esperado):
    assert queries.gs_protesto_de(nombre) == esperado


def test_el_monto_no_se_decide_por_el_numero_de_banco():
    """Los `no_banco` son de un catálogo legacy y no son estables.

    Pichincha es 10 en la data 2026 y era 1 en el dBase. Un número hardcodeado
    ya nos mandó un depósito a un banco inexistente (ch14778 BYG, 31/07).
    """
    src = inspect.getsource(queries.gs_protesto_de)
    assert not re.search(r"==\s*\d+", src), (
        "gs_protesto_de no puede comparar contra un no_banco: "
        "el catálogo cambia de números."
    )


# ---------------------------------------------------------------------------
# Los DOS caminos del protesto la emiten
# ---------------------------------------------------------------------------

def _fuente(fn) -> str:
    return inspect.getsource(fn)


def test_el_cambio_plano_a_devuelto_emite_la_comision():
    src = _fuente(queries.compensar_deposito_devuelto)
    assert "_insertar_gs_protesto" in src, (
        "Marcar devuelto un cheque depositado emite la ND pero no la comisión."
    )


def test_el_rebote_real_emite_la_comision():
    src = _fuente(queries.transicionar_stat)
    rebote = src[src.index('elif stat_destino == "9"'):]
    assert "_insertar_gs_protesto" in rebote, (
        "El asistente de rebote emite la ND pero no la comisión."
    )


# ---------------------------------------------------------------------------
# Lo que el dBase hace y NO se copia
# ---------------------------------------------------------------------------

def test_la_comision_no_nace_conciliada():
    """El dBase le pone `STAT '*'`; en PC '*' significa CONCILIADO.

    Copiarlo daría por cruzado con el banco un movimiento que nadie matcheó, y
    lo sacaría del panel de conciliación justo cuando aparece en el extracto.
    """
    src = _fuente(queries._insertar_gs_protesto)
    cuerpo = src[src.index("bank_helpers.insert_movimiento_bancario"):]
    assert "stat=" not in cuerpo, (
        "La comisión tiene que quedar en el stat default ('A', sin conciliar)."
    )


def test_la_comision_va_con_la_fecha_de_su_nota_de_debito():
    """El dBase la fecha con `FD` (el fechad del cheque, o sea el pasado).

    Insertar al medio deja el saldo running de todas las filas posteriores mal
    hasta correr `recompute_saldos_desde()`. Y el banco cobra el día del
    protesto, no el día en que el cheque estaba fechado.
    """
    # Sólo el CÓDIGO: la palabra "fechado" en la prosa no es una llamada.
    src = _fuente(queries._insertar_gs_protesto)
    codigo = src[src.index("bank_helpers.insert_movimiento_bancario"):]
    assert "fecha=fecha" in codigo
    assert "fechad" not in codigo


def test_el_concepto_es_el_del_dbase():
    """`GS. cheq. <cliente>` — es lo que la dueña busca en el extracto.

    Y es además el discriminante con el que los tests separan el gasto de la ND
    del cheque (las dos son 'ND' con el mismo `numreferencia`), así que vive en
    el módulo y no copiado en cada archivo.
    """
    assert queries.CONCEPTO_GS_PROTESTO == "GS. cheq."
    src = _fuente(queries._insertar_gs_protesto)
    assert "CONCEPTO_GS_PROTESTO" in src
