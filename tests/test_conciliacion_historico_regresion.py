"""Regresión del bug del cheque de 21k (BYG doc 14778) en la conciliación de banco.

El bug: un match cuyo counterpart es un HISTÓRICO del backlog (metodo =
'matched_historico') NO está en el extracto de la sesión. Si su doc-clave
(documento, monto, tipo) SIN fecha se usaba para EXCLUIR filas del extracto,
ocultaba la fila REAL (mismo doc+monto+tipo, otra fecha) → el lado banco quedaba
corto por ese importe y la diferencia NO cerraba (descuadre +$21.508,62).

El fix (SHA 455345e / _ya_conciliadas): la doc-clave `docs_real` se arma SOLO con
matches REALES (metodo != 'matched_historico'). La firma EXACTA (con fecha) sí
sigue cubriendo históricos = dedupe legítimo.

Este test bloquea la regresión en dos niveles:
  A) unidad — `_ya_conciliadas`: el histórico NO entra a docs_real; un match real SÍ.
  B) integración — `matchear_extracto_banco`: una fila real cuyo gemelo es un
     histórico SOBREVIVE (aparece como pendiente); una fila real cuyo gemelo es un
     match real se EXCLUYE.

Correr: pytest -m db tests/test_conciliacion_historico_regresion.py -q
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

NB = 1  # Pichincha
D = date(2099, 6, 15)  # fecha aislada (lejos de cualquier dato del dump)

DOC_HIST = "HIST0021508"
MONTO_HIST = Decimal("21508.62")
DOC_REAL = "REAL0001234"
MONTO_REAL = Decimal("1234.56")


def _seed_match(conn, *, documento, monto, fecha, tipo="D", metodo=None, id_transaccion=None):
    """Inserta una fila conciliada en banco_conciliacion_match."""
    conn.cursor().execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, real_fecha, real_concepto, real_documento,
             real_monto, real_tipo, id_transaccion, metodo, usuario)
        VALUES (%s, 'matched', %s, %s, %s, %s, %s, %s, %s, 'test')
        """,
        (NB, fecha, "regresion test", documento, monto, tipo, id_transaccion, metodo),
    )


@pytest.fixture
def setup(real_db_conn, migrated_db):
    cur = real_db_conn.cursor()
    cur.execute(
        "DELETE FROM scintela.banco_conciliacion_match "
        "WHERE real_documento IN (%s, %s)",
        (DOC_HIST, DOC_REAL),
    )
    cur.execute(
        "INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s,'PICHINCHA') "
        "ON CONFLICT (no_banco) DO NOTHING",
        (NB,),
    )
    # Gemelo HISTÓRICO (backlog): NO está en el extracto, id_transaccion NULL.
    _seed_match(conn=real_db_conn, documento=DOC_HIST, monto=MONTO_HIST, fecha=D,
                metodo="matched_historico", id_transaccion=None)
    # Gemelo REAL: match auto contra una tx del programa.
    _seed_match(conn=real_db_conn, documento=DOC_REAL, monto=MONTO_REAL, fecha=D,
                metodo="auto", id_transaccion=987654321)
    real_db_conn.commit()
    yield real_db_conn
    cur.execute(
        "DELETE FROM scintela.banco_conciliacion_match "
        "WHERE real_documento IN (%s, %s)",
        (DOC_HIST, DOC_REAL),
    )
    real_db_conn.commit()


# ── A) unidad: _ya_conciliadas ────────────────────────────────────────
@pytest.mark.db
def test_historico_no_entra_a_docs_excl_pero_el_real_si(setup):
    from modules.conciliacion.matcher_banco import _ya_conciliadas

    _ids, firmas_real, docs_real = _ya_conciliadas(NB, D, D)

    clave_hist = (DOC_HIST, f"{MONTO_HIST:.2f}", "D")
    clave_real = (DOC_REAL, f"{MONTO_REAL:.2f}", "D")

    # EL FIX: el histórico NO aporta doc-clave (no puede ocultar la fila real).
    assert clave_hist not in docs_real
    # Un match REAL SÍ aporta doc-clave (dedupe robusto al drift de fecha).
    assert clave_real in docs_real

    # La firma EXACTA (con fecha) sigue cubriendo AMBOS = dedupe legítimo.
    assert (D, DOC_HIST, f"{MONTO_HIST:.2f}", "D") in firmas_real
    assert (D, DOC_REAL, f"{MONTO_REAL:.2f}", "D") in firmas_real


# ── B) integración: matchear_extracto_banco ──────────────────────────
def _mov(fecha, documento, monto, tipo="D"):
    from modules.conciliacion.parser_banco import MovBanco

    return MovBanco(
        fecha=fecha, concepto="mov extracto", documento=documento,
        monto=monto, saldo=Decimal(0), codigo="", tipo=tipo, oficina="",
    )


@pytest.mark.db
def test_fila_real_gemela_de_historico_sobrevive_la_real_gemela_se_excluye(setup):
    from modules.conciliacion.matcher_banco import matchear_extracto_banco

    # Extracto con fecha DISTINTA (drift) a la del match, para forzar el camino
    # de la doc-clave SIN fecha (la firma exacta no coincide).
    fecha_extracto = D + timedelta(days=2)
    m_hist = _mov(fecha_extracto, DOC_HIST, MONTO_HIST)  # gemelo de un histórico
    m_real = _mov(fecha_extracto, DOC_REAL, MONTO_REAL)  # gemelo de un match real

    res = matchear_extracto_banco([m_hist, m_real], no_banco=NB)

    docs_pendientes = {m.documento for m in res.real_only}
    docs_matcheados = {mt.real.documento for mt in res.matches}
    vistos = docs_pendientes | docs_matcheados

    # La fila real cuyo gemelo es un HISTÓRICO NO debe desaparecer (era el 21k).
    assert DOC_HIST in docs_pendientes, (
        "La fila real gemela de un histórico fue ocultada — regresión del 21k"
    )
    # La fila real cuyo gemelo es un match REAL sí se excluye (ya conciliada).
    assert DOC_REAL not in vistos
