"""Anular un anticipo por error de carga tiene que llevarse su ESPEJO.

Caso real, 19/08/2026, cliente HOM. Alex cargó el depósito por $2.626,27 en
vez de $2.626,67, lo anuló por error de carga y lo recargó bien. La anulación
compensó el banco, reabrió las facturas y borró el posdatado — pero dejó vivo
el cheque ESPEJO NB=98 de −2.626,27 que el anticipo había creado.

Un espejo sin su padre no es medio movimiento: es una punta sola. HOM quedó
con un saldo a favor que nadie le debía y la utilidad del día bajó $2.626,27
sin contrapartida (se vio en /informes/traza como un renglón "CH HOM espejo
AN" con un solo número).

Lo que este archivo protege es la CUENTA: la posición del cliente antes del
anticipo y después de anularlo tiene que ser la misma. Y deshacer la anulación
tiene que devolver las DOS puntas, no una.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HOY = date.today()
CLI = "ESP"
IMPORTE = 2626.67


def _seed(conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo) "
        "VALUES (DEFAULT, %s, 'Cliente espejo', 'N', 30, 100000, TRUE) "
        "ON CONFLICT (codigo_cli) DO NOTHING",
        (CLI,),
    )
    for no_banco, nombre in ((1, "PICHINCHA"), (98, "ANTICIPO")):
        cur.execute(
            "INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s, %s) "
            "ON CONFLICT (no_banco) DO NOTHING",
            (no_banco, nombre),
        )


def _cartera(conn) -> float:
    """Lo que la empresa tiene por este cliente en cheques vivos."""
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(importe), 0) FROM scintela.cheque "
        " WHERE codigo_cli = %s "
        "   AND UPPER(TRIM(COALESCE(stat, ''))) IN ('Z','P','D','1','2','3')",
        (CLI,),
    )
    return round(float(cur.fetchone()[0]), 2)


def _stat(conn, id_cheque) -> str:
    cur = conn.cursor()
    cur.execute("SELECT stat FROM scintela.cheque WHERE id_cheque = %s", (id_cheque,))
    return (cur.fetchone()[0] or "").strip().upper()


@pytest.fixture
def setup(real_db_conn, migrated_db):
    # AUTOCOMMIT a propósito: una transacción abierta del test deja tomado un
    # ACCESS SHARE sobre scintela.cheque y el `bootstrap_columna()` que corre
    # el alta (ALTER TABLE ... IF NOT EXISTS) se queda esperándolo para
    # siempre. El síntoma es un test colgado, no un error.
    real_db_conn.autocommit = True
    cur = real_db_conn.cursor()
    cur.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    cur.execute("DELETE FROM scintela.cheque WHERE codigo_cli = %s", (CLI,))
    _seed(real_db_conn)
    real_db_conn.commit()
    return real_db_conn


def _anticipo_con_espejo(conn):
    """Un anticipo en cartera y su espejo negativo, como los crea la cobranza."""
    from modules.cheques import queries as chq

    row = chq.crear(
        fecha=HOY, codigo_cli=CLI, no_cheque="ESP001", importe=IMPORTE,
        no_banco=1, fechad=HOY, es_anticipo=True, usuario="test",
    )
    conn.commit()
    return int(row["id_cheque"]), int(row["id_cheque_anticipo"])


@pytest.mark.db
def test_el_espejo_se_anula_con_el_padre(setup):
    from modules.cheques import queries as chq

    conn = setup
    assert _cartera(conn) == 0.0, "el cliente arranca sin cheques"

    id_padre, id_espejo = _anticipo_con_espejo(conn)
    assert _stat(conn, id_espejo) == "Z"
    assert _cartera(conn) == 0.0, "anticipo + espejo se netean: no es plata nueva"

    res = chq.anular_por_error_de_carga(
        id_padre, motivo="importe mal cargado", usuario="test")
    conn.commit()

    assert _stat(conn, id_padre) == "X"
    assert _stat(conn, id_espejo) == "X", (
        "el espejo quedó vivo sin su padre: el cliente tiene un saldo a favor "
        "que nadie le debe"
    )
    assert res["espejos_anulados"] == [id_espejo]
    assert _cartera(conn) == 0.0, (
        f"anular movió la posición del cliente en {_cartera(conn)} — la "
        f"utilidad baja de una sola punta (caso HOM 19/08)"
    )


@pytest.mark.db
def test_el_renglon_del_espejo_deja_de_ofrecer_un_reverso_que_rebota(setup):
    from modules.cheques import queries as chq

    conn = setup
    id_padre, _ = _anticipo_con_espejo(conn)
    chq.anular_por_error_de_carga(id_padre, motivo="x", usuario="test")
    conn.commit()

    cur = conn.cursor()
    cur.execute(
        "SELECT estado, id_reverso FROM scintela.mov_doble "
        " WHERE tipo='cheque_anticipo_espejo' AND origen_id=%s", (id_padre,))
    estado, id_reverso = cur.fetchone()
    assert estado == "reversado", (
        "el ↺ del espejo seguía ofrecido y rebotaba con 'cheque ya cerrado'")
    assert id_reverso, "tachado pero sin decir cuál fue el reverso"


@pytest.mark.db
def test_deshacer_devuelve_las_dos_puntas(setup):
    from modules.cheques import queries as chq

    conn = setup
    id_padre, id_espejo = _anticipo_con_espejo(conn)
    chq.anular_por_error_de_carga(id_padre, motivo="x", usuario="test")
    conn.commit()

    cur = conn.cursor()
    cur.execute(
        "SELECT id_mov_doble FROM scintela.mov_doble "
        " WHERE tipo='reverso_cheque_administrativo' AND origen_id=%s "
        " ORDER BY id_mov_doble DESC LIMIT 1", (id_padre,))
    id_mov = int(cur.fetchone()[0])

    res = chq.deshacer_anulacion_error_carga(id_mov, usuario="test")
    conn.commit()

    assert _stat(conn, id_padre) == "Z"
    assert _stat(conn, id_espejo) == "Z", (
        "volvió el anticipo sin su espejo: ahora la utilidad SUBE de una sola "
        "punta — el mismo desbalance con el signo al revés"
    )
    assert res["espejos_revividos"] == [id_espejo]
    assert _cartera(conn) == 0.0


@pytest.mark.db
def test_no_se_lleva_puesto_al_cheque_de_REEMPLAZO(setup):
    """`id_cheque_padre` también ata el cheque nuevo al que reemplaza."""
    from modules.cheques import queries as chq

    conn = setup
    id_padre, _ = _anticipo_con_espejo(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, "
        "  fechad, stat, no_banco, id_cheque_padre) "
        "VALUES (%s, 'ESP002', %s, %s, %s, 'Z', 1, %s) RETURNING id_cheque",
        (HOY, CLI, IMPORTE, HOY, id_padre),
    )
    id_reemplazo = int(cur.fetchone()[0])
    conn.commit()

    chq.anular_por_error_de_carga(id_padre, motivo="x", usuario="test")
    conn.commit()

    assert _stat(conn, id_reemplazo) == "Z", (
        "el cheque bueno (el que reemplaza al mal cargado) se anuló solo")
