"""Tests de integración de flujos transaccionales — DB real.

Cubre el flujo "cheque rebotado → cliente a STOP" contra Postgres real, que
es más frágil que el test unit-level con stubs (tenemos una tx de verdad).

Correr con:
    pytest -m db -q
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest


def _seed_cliente(conn, codigo_cli: str = "TEST") -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo)
        VALUES (DEFAULT, %s, 'Cliente de prueba', 'N', 30, 10000, TRUE)
        ON CONFLICT (codigo_cli) DO NOTHING
    """,
        (codigo_cli,),
    )


def _seed_cheque(
    conn, *, codigo_cli: str, no_cheque: str = "1001", importe: float = 500.0, stat: str = "B"
) -> int:
    """Inserta un cheque en estado `stat` y devuelve su id."""
    cur = conn.cursor()
    hoy = date.today()
    cur.execute(
        """
        INSERT INTO scintela.cheque (
            fecha, no_cheque, codigo_cli, importe, fechad, stat, pasaconta
        ) VALUES (%s, %s, %s, %s, %s, %s, '0')
        RETURNING id_cheque
    """,
        (hoy, no_cheque, codigo_cli, importe, hoy - timedelta(days=1), stat),
    )
    return cur.fetchone()[0]


@pytest.mark.db
def test_reversar_cheque_depositado_anota_observacion_pero_no_aplica_stop(real_db_conn, migrated_db):
    """TMT 2026-05-21 dueña: el STOP es SOLO MANUAL.

    Flujo completo: cheque D → reversar → cliente NO pasa a STOP automático,
    pero la observación SÍ se anota con [REBOTE] para que la dueña pueda
    decidir manualmente.
    """
    _seed_cliente(real_db_conn, "TSTA")
    id_cheque = _seed_cheque(real_db_conn, codigo_cli="TSTA", stat="B", no_cheque="9001")
    real_db_conn.commit()

    from modules.cheques import queries as chq

    res = chq.reversar(id_cheque=id_cheque, motivo="Rechazado por banco", usuario="test")
    # El STOP NO se aplica automáticamente (decisión 2026-05-21).
    assert res["stop_aplicado"] is False
    assert res["es_rebote_real"] is True

    cur = real_db_conn.cursor()
    cur.execute(
        "SELECT stop, observacion FROM scintela.cliente WHERE codigo_cli=%s",
        ("TSTA",),
    )
    stop, observacion = cur.fetchone()
    # El cliente sigue activo (stop != 'S') — la dueña debe ponerlo manual.
    assert stop != "S"
    # Pero la observación debe contener el marker [REBOTE].
    assert "[REBOTE]" in (observacion or "")

    # Y el cheque debe quedar como primer rebote real.
    cur.execute("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))
    assert cur.fetchone()[0] == "1"


@pytest.mark.db
def test_reversar_cheque_en_cartera_no_aplica_stop(real_db_conn, migrated_db):
    """Cheque Z (en cartera, nunca depositado) — anulación administrativa."""
    _seed_cliente(real_db_conn, "TSTB")
    id_cheque = _seed_cheque(real_db_conn, codigo_cli="TSTB", stat="Z", no_cheque="9002")
    real_db_conn.commit()

    from modules.cheques import queries as chq

    res = chq.reversar(id_cheque=id_cheque, motivo="Carga errónea", usuario="test")
    assert res["es_rebote_real"] is False
    assert res["stop_aplicado"] is False

    cur = real_db_conn.cursor()
    cur.execute("SELECT stop FROM scintela.cliente WHERE codigo_cli=%s", ("TSTB",))
    stop = cur.fetchone()[0]
    # cliente sigue sin STOP
    assert stop != "S"


@pytest.mark.db
def test_reversar_cheque_ya_reversado_levanta(real_db_conn, migrated_db):
    """Reversar un cheque ya en stat R tiene que levantar ValueError."""
    _seed_cliente(real_db_conn, "TSTC")
    id_cheque = _seed_cheque(real_db_conn, codigo_cli="TSTC", stat="R", no_cheque="9003")
    real_db_conn.commit()

    from modules.cheques import queries as chq

    with pytest.raises(ValueError):
        chq.reversar(id_cheque=id_cheque, motivo="x", usuario="test")
