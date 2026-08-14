"""Casos de borde del rebote de cheques que el usuario PUEDE disparar hoy y que
tocan directamente el problema del cheque de 21k (doble conteo en el banco).

  #2  SEGUNDO REBOTE: depositar → rebota (1, ND #1) → RE-DEPOSITAR (V, DE #2) →
      vuelve a rebotar (2, ND #2). El banco debe descontar DOS veces (dos ND
      distintas), la factura NO se reabre en ninguno de los dos rebotes y la
      aplicación (chequesxfact) sigue viva. Alineado a dBase MODIFICA.PRG FIL3:
      un depositado (B/V/W) sólo rebota a 1/2 creando una ND — nunca a X.

  #3  IDEMPOTENCIA DE LA ND: doble-click en "marcar rebotado" (dos reversar
      seguidos sobre el mismo depósito) NO debe duplicar la ND en el banco.

Correr: pytest -m db tests/test_cheques_rebote_edge.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

HOY = date.today()


# ── helpers (mismos patrones que test_cheques_transiciones_usuario) ─────
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


def _seed_factura_aplicada(conn, *, codigo_cli, numf, importe, id_cheque, aplicado) -> int:
    cur = conn.cursor()
    abono = aplicado
    saldo = importe - abono
    stat = "T" if saldo <= 0.01 else "A"
    cur.execute(
        "INSERT INTO scintela.factura (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, tipo) "
        "VALUES (%s, %s, %s, 0, %s, %s, %s, %s, '1') RETURNING id_factura",
        (numf, HOY, codigo_cli, importe, abono, saldo, stat),
    )
    id_fact = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco) "
        "VALUES (%s, %s, %s, %s, %s, 1)",
        (id_cheque, id_fact, HOY, codigo_cli, aplicado),
    )
    return id_fact


def _stat(conn, id_cheque):
    cur = conn.cursor()
    cur.execute("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))
    return cur.fetchone()[0]


def _factura(conn, id_fact):
    cur = conn.cursor()
    cur.execute("SELECT abono, saldo, stat FROM scintela.factura WHERE id_factura=%s", (id_fact,))
    return cur.fetchone()


def _chequesxfact_count(conn, id_cheque):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.chequesxfact WHERE id_cheque=%s", (id_cheque,))
    return cur.fetchone()[0]


def _nd_count(conn, id_cheque):
    """Notas de débito (ND) COMPENSATORIAS emitidas para este cheque.

    TMT 2026-08-11: un cheque protestado genera DOS movimientos 'ND' con el
    mismo `numreferencia` — la que compensa el depósito y la del gasto que
    cobra el banco (`GS. cheq.`, paridad MODIFICA.PRG L314-318). Lo que estos
    tests protegen es que la COMPENSACIÓN no se duplique, así que el gasto se
    excluye por su concepto. El prefijo sale del módulo, no copiado acá: es el
    mismo filtro que usa tests/test_cheques_devuelto_compensa_banco.py.
    """
    from modules.cheques.queries import CONCEPTO_GS_PROTESTO

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias "
        "WHERE numreferencia=%s AND UPPER(TRIM(COALESCE(documento,'')))='ND' "
        "  AND COALESCE(concepto,'') NOT LIKE %s",
        (id_cheque, CONCEPTO_GS_PROTESTO + "%"),
    )
    return cur.fetchone()[0]


def _gs_count(conn, id_cheque):
    """El gasto bancario del protesto. Tiene que haber UNO por compensación."""
    from modules.cheques.queries import CONCEPTO_GS_PROTESTO

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias "
        "WHERE numreferencia=%s AND COALESCE(concepto,'') LIKE %s",
        (id_cheque, CONCEPTO_GS_PROTESTO + "%"),
    )
    return cur.fetchone()[0]


def _de_link_vivo(conn, id_cheque):
    """¿El cheque tiene todavía un link vivo a un depósito 'DE'?"""
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.chequextransaccion cxt "
        "JOIN scintela.transacciones_bancarias tb ON tb.id_transaccion=cxt.id_transaccion "
        "WHERE cxt.id_cheque=%s AND UPPER(TRIM(COALESCE(tb.documento,'')))='DE'",
        (id_cheque,),
    )
    return cur.fetchone()[0]


# ── Lo que este archivo llama "lo propio" ──────────────────────────────
# El banco es COMPARTIDO con los demás archivos de cheques: `assert_cadena_intacta`
# revisa la cadena de saldos del banco ENTERA antes de aceptar un alta.
CLI = "REB"          # el cliente que siembra este archivo
PREFIJO = "R000%"    # sus cheques: R0001, R0002
BANCO_PRUEBA = 1     # PICHINCHA


def _mis_movimientos_de_banco(cur):
    """Los movimientos bancarios que dejó una corrida ANTERIOR de este archivo.

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

    ⭐ TMT 2026-08-14 — este escenario NO netea a cero: cada protesto cobra un
    gasto de $2 ("GS. cheq. REB") que es plata que sale de verdad. Borrar el
    bloque a mano corría el saldo de TODAS las filas de abajo —las de los otros
    archivos de cheques, que depositan en el mismo banco— y el siguiente depósito
    moría en el candado de commit con "el saldo saltó 798,00 cuando el movimiento
    vale 800,00". Re-encadenar después de borrar es lo que hace la app cuando se
    elimina un movimiento (modules/bancos/queries.py).
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
    cur.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    cur.execute("DELETE FROM scintela.cheque WHERE no_cheque LIKE %s", (PREFIJO,))
    cur.execute("DELETE FROM scintela.factura WHERE codigo_cli = %s", (CLI,))
    if ids:
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


# ── #2 SEGUNDO REBOTE (depositar→rebota→re-deposita→rebota) ────────────
@pytest.mark.db
def test_segundo_rebote_V_crea_segunda_ND_y_no_reabre_factura(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="REB", no_cheque="R0001", importe=1000, stat="Z")
    id_fact = _seed_factura_aplicada(
        conn, codigo_cli="REB", numf=700001, importe=1000, id_cheque=id_ch, aplicado=1000
    )
    conn.commit()
    from modules.cheques import queries as chq

    # 1) Depositar (Z→B): crea el DE #1.
    chq.depositar_lote(ids_cheques=[id_ch], no_banco=1, usuario="test")
    assert _stat(conn, id_ch) == "B"
    assert _de_link_vivo(conn, id_ch) == 1

    # 2) Primer rebote (B→1): ND #1, se desagrupa el DE #1, factura intacta.
    chq.reversar(id_cheque=id_ch, motivo="rebote 1", usuario="test")
    assert _stat(conn, id_ch) == "1"
    assert _nd_count(conn, id_ch) == 1
    assert _de_link_vivo(conn, id_ch) == 0  # ya se compensó y desagrupó
    abono, _saldo, st = _factura(conn, id_fact)
    assert abs(float(abono) - 1000) < 0.01 and st == "T"  # factura NO reabierta
    assert _chequesxfact_count(conn, id_ch) == 1           # aplicación viva

    # 3) RE-DEPOSITAR el devuelto (1→V): crea el DE #2 (nuevo link vivo).
    chq.transicionar_stat(id_ch, stat_destino="V", usuario="test")
    assert _stat(conn, id_ch) == "V"
    assert _de_link_vivo(conn, id_ch) == 1

    # 4) SEGUNDO rebote (V→2): ND #2 (el banco descuenta OTRA vez), factura sigue
    #    sin reabrirse, aplicación sigue viva. Este es el caso que antes rompía:
    #    la ND #1 vieja hacía saltar la ND #2 y el 2° depósito quedaba doble.
    r = chq.reversar(id_cheque=id_ch, motivo="rebote 2", usuario="test")
    assert r["es_rebote_real"] is True
    assert _stat(conn, id_ch) == "2"          # vuelve a cartera como 2° devuelto
    assert _nd_count(conn, id_ch) == 2         # DOS ND distintas
    assert _de_link_vivo(conn, id_ch) == 0     # el DE #2 también se compensó
    abono, _saldo, st = _factura(conn, id_fact)
    assert abs(float(abono) - 1000) < 0.01 and st == "T"  # factura NO reabierta
    assert _chequesxfact_count(conn, id_ch) == 1           # aplicación viva


# ── #3 IDEMPOTENCIA DE LA ND (doble-click en "marcar rebotado") ────────
@pytest.mark.db
def test_ND_no_se_duplica_con_doble_reversar(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="REB", no_cheque="R0002", importe=1000, stat="Z")
    conn.commit()
    from modules.cheques import queries as chq

    # Depositar y rebotar (crea la ND #1 y desagrupa el DE).
    chq.depositar_lote(ids_cheques=[id_ch], no_banco=1, usuario="test")
    chq.reversar(id_cheque=id_ch, motivo="rebote", usuario="test")
    assert _stat(conn, id_ch) == "1"
    assert _nd_count(conn, id_ch) == 1

    # Doble-click: un SEGUNDO reversar sobre el mismo depósito ya compensado.
    # No debe crear una segunda ND (ya no hay DE vivo que compensar).
    chq.reversar(id_cheque=id_ch, motivo="rebote (doble click)", usuario="test")
    assert _nd_count(conn, id_ch) == 1  # sigue habiendo UNA sola ND
