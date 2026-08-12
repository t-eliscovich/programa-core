"""Cancelar por anticipo un cheque YA APLICADO a facturas no mueve la plata.

Alex, 12/08/2026, cliente GLI: trae $11.700,52 en efectivo para levantar tres
cheques a fecha que ya tenía dados, y la pantalla lo frena con *"El cheque
#101594 ya está aplicado a factura(s) — desaplicalo desde su ficha"*.

Ese freno lo escribió el mismo commit que creó la función (`6429255e`, 06/07)
diciendo que cancelar "reabriría las facturas sin compensación del anticipo".
No es cierto: `cancelar_por_anticipo` no toca `chequesxfact` ni los saldos —
marca el cheque 'X', borra el posdatado hermano y registra el mov_doble.

Y el caso es el normal: el cliente había pagado con cheques a fecha (la factura
ya figura cancelada), ahora paga en efectivo y se lleva los cheques. La factura
sigue pagada; lo que cambia es qué la respalda.

Lo que este archivo protege es la CUENTA, no el permiso: después de cancelar,
la posición del cliente tiene que quedar igual.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HOY = date.today()
CLI = "ANT"


def _seed_cliente(conn, codigo_cli):
    conn.cursor().execute(
        "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo) "
        "VALUES (DEFAULT, %s, 'Cliente anticipo', 'N', 30, 100000, TRUE) "
        "ON CONFLICT (codigo_cli) DO NOTHING",
        (codigo_cli,),
    )


def _seed_banco(conn, no_banco, nombre):
    conn.cursor().execute(
        "INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s, %s) "
        "ON CONFLICT (no_banco) DO NOTHING",
        (no_banco, nombre),
    )


def _posicion(conn, codigo_cli):
    """Lo que la empresa tiene por este cliente: cartera + facturas abiertas."""
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(importe), 0) FROM scintela.cheque "
        " WHERE codigo_cli = %s "
        "   AND UPPER(TRIM(COALESCE(stat, ''))) IN ('Z','P','D','1','2','3')",
        (codigo_cli,),
    )
    cartera = float(cur.fetchone()[0])
    cur.execute(
        "SELECT COALESCE(SUM(saldo), 0) FROM scintela.factura "
        " WHERE codigo_cli = %s AND UPPER(TRIM(COALESCE(stat, ''))) NOT IN ('X','T')",
        (codigo_cli,),
    )
    return cartera + float(cur.fetchone()[0])


@pytest.fixture
def setup(real_db_conn, migrated_db):
    cur = real_db_conn.cursor()
    cur.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    cur.execute("DELETE FROM scintela.cheque WHERE codigo_cli = %s", (CLI,))
    cur.execute("DELETE FROM scintela.factura WHERE codigo_cli = %s", (CLI,))
    _seed_cliente(real_db_conn, CLI)
    _seed_banco(real_db_conn, 1, "PICHINCHA")
    real_db_conn.commit()
    return real_db_conn


def _armar_caso(conn):
    """Una factura de 4.000 pagada con un cheque a fecha, todavía en cartera."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad, "
        "                             stat, no_banco, pasaconta) "
        "VALUES (%s, 'A00291', %s, 4000, %s, 'Z', 1, '0') RETURNING id_cheque",
        (HOY, CLI, HOY + timedelta(days=9)),
    )
    id_cheque = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO scintela.factura (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, tipo) "
        "VALUES (900291, %s, %s, 0, 4000, 4000, 0, 'T', '1') RETURNING id_factura",
        (HOY, CLI),
    )
    id_factura = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO scintela.chequesxfact "
        "  (id_cheque, id_fact, codigo_cli, importe, fechaing, usuario_crea, "
        "   abono_f, saldo_f, stat_f) "
        "VALUES (%s, %s, %s, 4000, %s, 'test', 4000, 0, 'T')",
        (id_cheque, id_factura, CLI, HOY),
    )
    conn.commit()
    return id_cheque, id_factura


@pytest.mark.db
def test_se_puede_cancelar_un_cheque_aplicado(setup):
    """El freno del 06/07 bloqueaba el caso normal. Ya no está."""
    from modules.cheques import queries as chq

    conn = setup
    id_cheque, _ = _armar_caso(conn)
    chq.cancelar_por_anticipo(
        id_cheque=id_cheque, codigo_cli=CLI,
        id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
    )
    cur = conn.cursor()
    cur.execute("SELECT stat FROM scintela.cheque WHERE id_cheque = %s", (id_cheque,))
    assert (cur.fetchone()[0] or "").strip().upper() == "X"


@pytest.mark.db
def test_la_factura_sigue_pagada(setup):
    """Cancelar el cheque NO reabre la factura — era la premisa del freno viejo."""
    from modules.cheques import queries as chq

    conn = setup
    id_cheque, id_factura = _armar_caso(conn)
    chq.cancelar_por_anticipo(
        id_cheque=id_cheque, codigo_cli=CLI,
        id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT abono, saldo, stat FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    abono, saldo, stat = cur.fetchone()
    assert float(abono) == 4000.0
    assert float(saldo) == 0.0
    assert (stat or "").strip().upper() == "T"


@pytest.mark.db
def test_la_plata_del_cliente_no_se_mueve(setup):
    """⭐ El invariante: el cheque sale de cartera y nada más cambia.

    Antes: factura pagada + un cheque de 4.000 en cartera.
    Después: factura pagada + el cheque afuera. La posición baja exactamente
    el importe del cheque, que es lo que el cliente se llevó — y que en la
    operación real entra de vuelta como el anticipo que lo cancela.
    """
    from modules.cheques import queries as chq

    conn = setup
    id_cheque, _ = _armar_caso(conn)
    antes = _posicion(conn, CLI)
    chq.cancelar_por_anticipo(
        id_cheque=id_cheque, codigo_cli=CLI,
        id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
    )
    despues = _posicion(conn, CLI)
    assert antes - despues == pytest.approx(4000.0, abs=0.01), (
        f"la posición del cliente pasó de {antes} a {despues}: cancelar el "
        "cheque movió algo más que el cheque"
    )


@pytest.mark.db
def test_los_guards_que_SI_importan_siguen(setup):
    """Sacar el freno de las aplicaciones no aflojó los otros."""
    from modules.cheques import queries as chq

    conn = setup
    id_cheque, _ = _armar_caso(conn)
    with pytest.raises(ValueError, match="no es de|no de"):
        chq.cancelar_por_anticipo(
            id_cheque=id_cheque, codigo_cli="OTRO",
            id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
        )
    chq.cancelar_por_anticipo(
        id_cheque=id_cheque, codigo_cli=CLI,
        id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
    )
    # ya está en X: no se puede cancelar dos veces
    with pytest.raises(ValueError, match="sólo se|stat="):
        chq.cancelar_por_anticipo(
            id_cheque=id_cheque, codigo_cli=CLI,
            id_cheque_anticipo=None, monto_anticipo=4000.0, usuario="test",
        )
