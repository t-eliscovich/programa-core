"""Bordes de la máquina de estados del cheque:

  A. TRANSICIONES ILEGALES — `transicionar_stat` RECHAZA (ValueError) cualquier
     par (origen→destino) que no esté en TRANSICIONES_VALIDAS, sin tocar la DB.
     Sin esto, un usuario podría dejar un cheque en un estado imposible.

  B. LOTE MIXTO — `depositar_lote` con un batch que mezcla cartera (Z) y devueltos
     (1) en la misma llamada: el Z queda 'B' y el devuelto queda 'V' (dBase
     DEPOBAN), ambos ligados al mismo depósito consolidado 'DE'.

Correr: pytest -m db tests/test_cheques_state_machine_edge.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

HOY = date.today()


def _seed_cliente(conn, codigo_cli: str) -> None:
    conn.cursor().execute(
        "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo) "
        "VALUES (DEFAULT, %s, 'Cliente de prueba', 'N', 30, 100000, TRUE) "
        "ON CONFLICT (codigo_cli) DO NOTHING",
        (codigo_cli,),
    )


def _seed_banco(conn, no_banco: int, nombre: str) -> None:
    conn.cursor().execute(
        "INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s, %s) "
        "ON CONFLICT (no_banco) DO NOTHING",
        (no_banco, nombre),
    )


def _seed_cheque(conn, *, codigo_cli, no_cheque, importe=500.0, stat="Z", no_banco=1) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad, "
        "                             stat, no_banco, pasaconta) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, '0') RETURNING id_cheque",
        (HOY, no_cheque, codigo_cli, importe, HOY - timedelta(days=1), stat, no_banco),
    )
    return cur.fetchone()[0]


def _stat(conn, id_cheque):
    cur = conn.cursor()
    cur.execute("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))
    return cur.fetchone()[0]


# ── Lo que este archivo llama "lo propio" ──────────────────────────────
# El banco es COMPARTIDO con los demás archivos de cheques: `assert_cadena_intacta`
# revisa la cadena de saldos del banco ENTERA antes de aceptar cualquier alta, así
# que una limpieza que borre de más (o de menos) no ensucia sólo a este archivo:
# le bloquea el depósito a todos los que corran después.
CLI = "SME"          # el cliente que siembra este archivo
PREFIJO = "S00%"     # sus cheques: S000Z1, S000D1, S00XB, S00C1, S00BC, S00VB, S0031
BANCO_PRUEBA = 1     # PICHINCHA


def _mis_movimientos_de_banco(cur):
    """Los movimientos bancarios que dejó una corrida ANTERIOR de este archivo.

    TMT 2026-08-14 — no se los puede reconocer por el TEXTO. El depósito en lote
    consolida N cheques en UNA fila cuyo concepto es "dep.N ch.", sin número de
    cheque ni cliente adentro: cualquier `LIKE` lo bastante ancho para atraparla
    atrapa también la de los otros archivos. Los dos vínculos firmes son
    `chequextransaccion` (los depósitos) y `numreferencia`, que lleva el id del
    cheque (la ND del rebote y el GS del protesto).

    Devuelve (ids, fecha_más_vieja): la fecha es el ancla del re-encadenado.
    """
    cur.execute(
        """
        SELECT t.id_transaccion, t.fecha
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s
           AND (t.numreferencia IN (SELECT id_cheque FROM scintela.cheque
                                     WHERE no_cheque LIKE %s)
                OR t.id_transaccion IN (SELECT id_transaccion
                                          FROM scintela.chequextransaccion
                                         WHERE id_cheque IN (
                                               SELECT id_cheque FROM scintela.cheque
                                                WHERE no_cheque LIKE %s)))
        """,
        (BANCO_PRUEBA, PREFIJO, PREFIJO),
    )
    filas = cur.fetchall()
    return [f[0] for f in filas], (min(f[1] for f in filas) if filas else None)


def _limpiar(conn) -> None:
    """Borra lo que sembró ESTE archivo y deja la cadena del banco sana.

    ⭐ TMT 2026-08-14 — POR QUÉ ESTO NO ES UN DETALLE. Acá decía
    `DELETE ... WHERE concepto LIKE 'dep.% ch.%'`, que no es una marca propia:
    se comía el depósito de `test_cheques_rebote_edge.py` ("dep.1 ch. R0002 REB")
    y le dejaba la ND huérfana. Con el DE borrado y la ND viva el saldo de
    Pichincha quedaba partido, y a partir de ahí `assert_cadena_intacta` rebotaba
    TODO depósito posterior: 9 tests en rojo apenas la suite se corría dos veces
    contra la misma base. En la primera corrida no se veía porque esa ND huérfana
    era la PRIMERA fila del banco, y una fila sin predecesora no puede mostrar un
    salto de saldo.
    """
    cur = conn.cursor()
    ids, desde = _mis_movimientos_de_banco(cur)
    cur.execute(
        "DELETE FROM scintela.chequextransaccion WHERE id_cheque IN "
        "(SELECT id_cheque FROM scintela.cheque WHERE no_cheque LIKE %s)",
        (PREFIJO,),
    )
    if ids:
        cur.execute("DELETE FROM scintela.chequextransaccion WHERE id_transaccion = ANY(%s)", (ids,))
        cur.execute("DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s)", (ids,))
    cur.execute("DELETE FROM scintela.cheque WHERE no_cheque LIKE %s", (PREFIJO,))
    if ids:
        # Sacar una fila del MEDIO deja a las de abajo con el saldo del estado
        # viejo. Re-encadenar es lo que hace la app cuando se elimina un
        # movimiento (modules/bancos/queries.py); si el test borra a mano y no
        # re-encadena, el próximo depósito muere en el candado de commit.
        import bank_helpers

        bank_helpers.recompute_saldos_desde(conn, no_banco=BANCO_PRUEBA, ancla_fecha=desde)
    conn.commit()


@pytest.fixture
def setup(real_db_conn, migrated_db):
    _limpiar(real_db_conn)
    _seed_cliente(real_db_conn, CLI)
    _seed_banco(real_db_conn, BANCO_PRUEBA, "PICHINCHA")
    _seed_banco(real_db_conn, 2, "INTERNACIONAL")
    real_db_conn.commit()
    return real_db_conn


# ── A. TRANSICIONES ILEGALES ───────────────────────────────────────────
# (origen, destino) que NO están en TRANSICIONES_VALIDAS y deben tirar ValueError.
CASOS_ILEGALES = [
    ("X", "B"),   # anulado no se deposita (X → sólo Z/P/D)
    ("C", "1"),   # efectivo no rebota (C no tiene destinos)
    ("B", "C"),   # depositado no pasa a efectivo (B → sólo 9/X/1)
    ("V", "B"),   # re-depósito no se re-deposita como B (V → sólo 1)
    ("3", "1"),   # 3er devuelto no baja a 1 (3 → sólo Z/P/D/X)
]


@pytest.mark.db
@pytest.mark.parametrize("origen,destino", CASOS_ILEGALES)
def test_transicion_ilegal_rechazada_sin_mutar(setup, origen, destino):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="SME", no_cheque=f"S00{origen}{destino}", stat=origen)
    conn.commit()
    from modules.cheques import queries as chq

    with pytest.raises(ValueError, match="no permitida"):
        chq.transicionar_stat(id_ch, stat_destino=destino, usuario="test")
    # La DB NO cambió: el cheque sigue en el estado de origen.
    assert _stat(conn, id_ch) == origen


# ── A2. "Cobrar en efectivo" (C) presente en el dropdown ───────────────
def test_dropdown_ofrece_efectivo_donde_el_backend_lo_permite():
    """El dropdown (transiciones_para) tiene que ofrecer 'Cobrar en efectivo' (C)
    en los estados de cartera donde el backend lo permite — dBase lo deja
    (MODIFICA.PRG cartera→C → caja). TMT 2026-07-26. Antes faltaba (bug de UI:
    el backend aceptaba Z→C pero el dropdown nunca lo generaba)."""
    from modules.cheques import queries as chq

    # TMT 2026-08-11: los estados NO van tipeados. El invariante es que el
    # dropdown y el backend digan lo mismo — cuáles sean depende de la matriz,
    # y la matriz cambia por decisión de la dueña (ese día habilitó cobrar en
    # efectivo un cheque DEVUELTO, 1/2/3→C, que el dBase ya dejaba). Con la
    # lista escrita a mano, ese cambio rompía un test que no habla de eso.
    for s in chq.TRANSICIONES_VALIDAS:
        dests = {o["stat_destino"] for o in chq.transiciones_para(s)}
        permite = "C" in chq.TRANSICIONES_VALIDAS.get(s, set())
        assert ("C" in dests) == permite, (
            f"desde {s!r}: el backend {'permite' if permite else 'NO permite'} "
            f"cobrar en efectivo y el dropdown dice lo contrario"
        )


# ── B. DEPOSITAR LOTE MIXTO (Z + devuelto en la misma llamada) ─────────
@pytest.mark.db
def test_depositar_lote_mixto_Z_da_B_y_devuelto_da_V(setup):
    conn = setup
    id_z = _seed_cheque(conn, codigo_cli="SME", no_cheque="S000Z1", importe=500, stat="Z")
    id_d = _seed_cheque(conn, codigo_cli="SME", no_cheque="S000D1", importe=300, stat="1")
    conn.commit()
    from modules.cheques import queries as chq

    res = chq.depositar_lote(ids_cheques=[id_z, id_d], no_banco=1, usuario="test")

    # Cada cheque queda con la letra que RECUERDA su origen (dBase DEPOBAN).
    assert _stat(conn, id_z) == "B"   # venía de cartera
    assert _stat(conn, id_d) == "V"   # venía de devuelto → re-depósito

    # Un ÚNICO depósito consolidado 'DE' por el total del lote (500+300),
    # con los dos cheques ligados vía chequextransaccion.
    cur = conn.cursor()
    ids_t = res["id_transacciones"]
    assert len(ids_t) == 1
    cur.execute("SELECT documento, importe FROM scintela.transacciones_bancarias "
                "WHERE id_transaccion=%s", (ids_t[0],))
    doc, importe = cur.fetchone()
    assert doc.strip() == "DE" and abs(float(importe) - 800) < 0.01
    cur.execute("SELECT COUNT(*) FROM scintela.chequextransaccion WHERE id_transaccion=%s "
                "AND id_cheque IN (%s,%s)", (ids_t[0], id_z, id_d))
    assert cur.fetchone()[0] == 2
