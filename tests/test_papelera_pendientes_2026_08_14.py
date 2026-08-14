"""Borrar un pendiente y traerlo de vuelta.

TMT 2026-08-14. Este camino estuvo ROTO desde junio y no lo cazó nadie: la
migración `0064_histos_codigo` nunca corrió (su número ya lo usaba otra), así
que `banco_historicos_pendientes` no tenía la columna `codigo` y el INSERT de
restaurar la nombraba igual. Se arregló ayer renumerando la migración a 0190.

No tenía un solo test. Estos cubren la ida y la vuelta, y sobre todo que
`restaurar` siga nombrando `codigo`: es la garantía de que si la columna
volviera a faltar, salta acá y no en la cara de Alex.

Ojo con lo que hace restaurar: MUEVE la conciliación. El pendiente vuelve a
sumar al "Saldo banco esperado" por su importe.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

NO_BANCO = 78


def _fila(**kw):
    base = {"fecha": date(2026, 8, 11), "concepto": "DEPOSITO",
            "documento": "41508270", "monto": 590.27, "tipo": "C",
            "oficina": "AG. NORTE", "detalle": "", "codigo": "001045"}
    base.update(kw)
    return base


@pytest.fixture
def setup(real_db_conn, migrated_db):
    cur = real_db_conn.cursor()
    cur.execute("DELETE FROM scintela.banco_pendientes_papelera WHERE no_banco = %s",
                (NO_BANCO,))
    cur.execute("DELETE FROM scintela.banco_historicos_pendientes WHERE no_banco = %s",
                (NO_BANCO,))
    real_db_conn.commit()
    return real_db_conn


def _pendientes(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.banco_historicos_pendientes "
                " WHERE no_banco = %s", (NO_BANCO,))
    return cur.fetchone()[0]


@pytest.mark.db
def test_lo_borrado_se_puede_traer_de_vuelta(setup):
    """La ida y la vuelta completas: es el caso que estuvo roto dos meses."""
    from modules.conciliacion import papelera_pendientes as _pap

    id_pap = _pap.guardar(_fila(), no_banco=NO_BANCO, usuario="alex")
    setup.commit()
    assert id_pap, "no guardó en la papelera"
    assert _pendientes(setup) == 0

    r = _pap.restaurar(id_pap, no_banco=NO_BANCO, usuario="alex")
    setup.commit()

    assert r.get("ok") is True, f"no restauró: {r.get('motivo')}"
    assert float(r.get("monto") or 0) == 590.27, "tiene que devolver el importe"
    assert _pendientes(setup) == 1, "el pendiente no volvió al backlog"


@pytest.mark.db
def test_restaurar_guarda_el_codigo(setup):
    """El `codigo` es lo que distingue un cheque devuelto de su IVA y de su
    costo cuando comparten documento. Si se pierde al restaurar, el dedupe
    del próximo extracto puede colapsar filas legítimas."""
    from modules.conciliacion import papelera_pendientes as _pap

    id_pap = _pap.guardar(_fila(codigo="098426"), no_banco=NO_BANCO,
                          usuario="alex")
    setup.commit()
    _pap.restaurar(id_pap, no_banco=NO_BANCO, usuario="alex")
    setup.commit()

    cur = setup.cursor()
    cur.execute("SELECT codigo FROM scintela.banco_historicos_pendientes "
                " WHERE no_banco = %s", (NO_BANCO,))
    assert cur.fetchone()[0] == "098426"


@pytest.mark.db
def test_no_se_restaura_dos_veces(setup):
    """El segundo intento tiene que decir que no, no duplicar el pendiente
    (que sumaría su importe de nuevo al saldo esperado)."""
    from modules.conciliacion import papelera_pendientes as _pap

    id_pap = _pap.guardar(_fila(), no_banco=NO_BANCO, usuario="alex")
    setup.commit()
    _pap.restaurar(id_pap, no_banco=NO_BANCO, usuario="alex")
    setup.commit()

    r2 = _pap.restaurar(id_pap, no_banco=NO_BANCO, usuario="alex")
    setup.commit()
    assert r2.get("ok") is not True, "restauró dos veces la misma fila"
    assert _pendientes(setup) == 1, "duplicó el pendiente"


@pytest.mark.db
def test_restaurar_algo_que_no_existe_no_rompe(setup):
    from modules.conciliacion import papelera_pendientes as _pap

    r = _pap.restaurar(999999, no_banco=NO_BANCO, usuario="alex")
    assert r.get("ok") is not True
    assert r.get("motivo"), "tiene que decir por qué no pudo"


@pytest.mark.db
def test_la_papelera_lista_lo_borrado(setup):
    from modules.conciliacion import papelera_pendientes as _pap

    _pap.guardar(_fila(documento="A1"), no_banco=NO_BANCO, usuario="alex")
    _pap.guardar(_fila(documento="A2"), no_banco=NO_BANCO, usuario="alex")
    setup.commit()

    docs = {str(f.get("documento")) for f in _pap.listar(NO_BANCO)}
    assert {"A1", "A2"} <= docs
