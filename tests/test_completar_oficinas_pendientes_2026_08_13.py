"""Completar la OFICINA de los pendientes desde los extractos ya guardados.

TMT 2026-08-13. La dueña: *"necesito que me pongas oficina, no lo veo en la
conciliación actual"*. Los 189 pendientes del Pichincha vienen todos de la
hoja FEB2023, que nunca tuvo columna de oficina — medido en producción: 0 de
189 con oficina. Pero el MISMO movimiento sí aparece con su oficina en los
extractos del banco que se subieron en sesiones anteriores y quedaron en
`banco_conciliacion_sesion.extracto_payload` (2.855 filas, todas con
oficina). Cruzando por fecha + documento + monto + tipo se pueden completar
159 de los 189.

Estos tests corren contra Postgres de verdad porque lo que hay que probar es
el UPDATE ... FROM con el jsonb, no un mock.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

NO_BANCO = 77


def _mov(documento, monto, oficina, fecha="2026-07-15", tipo="C"):
    return {"fecha": fecha, "concepto": "TRANSFERENCIA", "documento": documento,
            "monto": monto, "saldo": 0, "codigo": "001045", "tipo": tipo,
            "oficina": oficina}


def _sesion_con(conn, movs):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.banco_conciliacion_sesion "
        "  (no_banco, usuario, extracto_nombre, extracto_payload) "
        "VALUES (%s, 'test', 'mov.xlsx', %s::jsonb) RETURNING id",
        (NO_BANCO, json.dumps(movs)),
    )
    return cur.fetchone()[0]


def _pendiente(conn, documento, monto, oficina=None, fecha="2026-07-15", tipo="C"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.banco_historicos_pendientes "
        "  (no_banco, fecha, concepto, documento, monto, tipo, oficina, fuente) "
        "VALUES (%s, %s, 'DEPOSITO', %s, %s, %s, %s, 'test') RETURNING id",
        (NO_BANCO, fecha, documento, monto, tipo, oficina),
    )
    return cur.fetchone()[0]


def _oficina_de(conn, id_pend):
    cur = conn.cursor()
    cur.execute(
        "SELECT oficina FROM scintela.banco_historicos_pendientes WHERE id = %s",
        (id_pend,),
    )
    return cur.fetchone()[0]


@pytest.fixture
def setup(real_db_conn, migrated_db):
    cur = real_db_conn.cursor()
    cur.execute("DELETE FROM scintela.banco_historicos_pendientes WHERE no_banco = %s",
                (NO_BANCO,))
    cur.execute("DELETE FROM scintela.banco_conciliacion_sesion WHERE no_banco = %s",
                (NO_BANCO,))
    real_db_conn.commit()
    return real_db_conn


@pytest.mark.db
def test_le_pone_la_oficina_al_pendiente_que_matchea(setup):
    from modules.conciliacion import sesion as _s

    _sesion_con(setup, [_mov("41508270", 590.27, "AG. NORTE")])
    pid = _pendiente(setup, "41508270", 590.27)
    setup.commit()

    assert _s.completar_oficinas_pendientes(NO_BANCO) == 1
    assert _oficina_de(setup, pid) == "AG. NORTE"


@pytest.mark.db
def test_no_pisa_una_oficina_que_ya_estaba_cargada(setup):
    from modules.conciliacion import sesion as _s

    _sesion_con(setup, [_mov("41508270", 590.27, "AG. NORTE")])
    pid = _pendiente(setup, "41508270", 590.27, oficina="CALDERON")
    setup.commit()

    assert _s.completar_oficinas_pendientes(NO_BANCO) == 0
    assert _oficina_de(setup, pid) == "CALDERON", "no toca lo ya cargado"


@pytest.mark.db
def test_si_el_cruce_es_ambiguo_no_elige_ninguna(setup):
    """Dos filas con la MISMA firma y oficinas distintas: se saltea.

    Sin el HAVING la elección sería arbitraria y le pondría a la dueña una
    oficina que capaz no es. Mejor vacía que inventada.
    """
    from modules.conciliacion import sesion as _s

    _sesion_con(setup, [_mov("999", 100.0, "AG. NORTE"),
                        _mov("999", 100.0, "CALDERON")])
    pid = _pendiente(setup, "999", 100.0)
    setup.commit()

    assert _s.completar_oficinas_pendientes(NO_BANCO) == 0
    assert _oficina_de(setup, pid) is None


@pytest.mark.db
def test_no_cruza_movimientos_de_distinto_monto_ni_fecha(setup):
    from modules.conciliacion import sesion as _s

    _sesion_con(setup, [_mov("41508270", 590.27, "AG. NORTE", fecha="2026-07-15")])
    otro_monto = _pendiente(setup, "41508270", 590.28)
    otra_fecha = _pendiente(setup, "41508270", 590.27, fecha="2026-07-16")
    otro_tipo = _pendiente(setup, "41508270", 590.27, tipo="D")
    setup.commit()

    assert _s.completar_oficinas_pendientes(NO_BANCO) == 0
    assert _oficina_de(setup, otro_monto) is None
    assert _oficina_de(setup, otra_fecha) is None
    assert _oficina_de(setup, otro_tipo) is None
