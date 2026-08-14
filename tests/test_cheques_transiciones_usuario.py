"""Prueba end-to-end de CADA operación que el usuario hace con un cheque,
una por una, contra Postgres real. Verifica el comportamiento alineado a dBase
(SHA a30ccb8 / 56d0b3f):

  1. Depositar cartera (Z → B)  → crea movimiento de banco (DE)
  2. Marcar devuelto directo (Z → 1)
  3. Re-depositar un devuelto individual (1 → V) → crea movimiento de banco (DE)
  4. Depositar lote un devuelto (1 → V vía depositar_lote)
  5. Rebote real (B → 1 vía reversar) → ND en banco, NO reabre la factura
  6. Anular por error de carga (Z → X vía reversar) → SÍ reabre la factura
  7. Postergar (Z → P)
  8. Cobrar en efectivo (Z → C) → movimiento de caja

Correr: pytest -m db tests/test_cheques_transiciones_usuario.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

HOY = date.today()


def _seed_cliente(conn, codigo_cli: str) -> None:
    conn.cursor().execute(
        """
        INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo)
        VALUES (DEFAULT, %s, 'Cliente de prueba', 'N', 30, 100000, TRUE)
        ON CONFLICT (codigo_cli) DO NOTHING
        """,
        (codigo_cli,),
    )


def _seed_banco(conn, no_banco: int, nombre: str) -> None:
    conn.cursor().execute(
        """
        INSERT INTO scintela.banco (no_banco, nombre)
        VALUES (%s, %s) ON CONFLICT (no_banco) DO NOTHING
        """,
        (no_banco, nombre),
    )


def _seed_cheque(conn, *, codigo_cli, no_cheque, importe=500.0, stat="Z", no_banco=1) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad,
                                     stat, no_banco, pasaconta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, '0')
        RETURNING id_cheque
        """,
        (HOY, no_cheque, codigo_cli, importe, HOY - timedelta(days=1), stat, no_banco),
    )
    return cur.fetchone()[0]


def _seed_factura_aplicada(conn, *, codigo_cli, numf, importe, id_cheque, aplicado) -> int:
    """Crea una factura y le APLICA un cheque: factura.abono += aplicado,
    saldo -= aplicado, + fila en chequesxfact. Devuelve id_factura."""
    cur = conn.cursor()
    abono = aplicado
    saldo = importe - abono
    stat = "T" if saldo <= 0.01 else "A"
    cur.execute(
        """
        INSERT INTO scintela.factura (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, tipo)
        VALUES (%s, %s, %s, 0, %s, %s, %s, %s, '1')
        RETURNING id_factura
        """,
        (numf, HOY, codigo_cli, importe, abono, saldo, stat),
    )
    id_fact = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco)
        VALUES (%s, %s, %s, %s, %s, 1)
        """,
        (id_cheque, id_fact, HOY, codigo_cli, aplicado),
    )
    return id_fact


def _banco_movs(conn, id_cheque, no_cheque, documento):
    """Movimientos de banco (transacciones_bancarias) generados para ese cheque."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT importe, documento, concepto FROM scintela.transacciones_bancarias
         WHERE documento = %s AND concepto ILIKE %s
        """,
        (documento, f"%{no_cheque}%"),
    )
    return cur.fetchall()


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


# ── Lo que este archivo llama "lo propio" ──────────────────────────────
# El banco es COMPARTIDO con los demás archivos de cheques: `assert_cadena_intacta`
# revisa la cadena de saldos del banco ENTERA antes de aceptar un alta, así que una
# limpieza que borre de más (o de menos) le bloquea el depósito a todos los que
# corran después, no sólo a este archivo.
CLI = "USR"          # el cliente que siembra este archivo
PREFIJO = "T000%"    # sus cheques: T0001..T0008 (los del smoke son T001..T039)
BANCO_PRUEBA = 1     # PICHINCHA


def _mis_movimientos_de_banco(cur):
    """Los movimientos bancarios que dejó una corrida ANTERIOR de este archivo.

    TMT 2026-08-14 — acá se filtraba por `concepto LIKE '%T000%'`, que deja
    afuera dos filas que este archivo SÍ crea: el depósito en lote consolidado
    ("dep.N ch.", sin número de cheque adentro) y el gasto del protesto
    ("GS. cheq. USR"). Los dos vínculos firmes son `chequextransaccion` y
    `numreferencia`, que lleva el id del cheque.

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
    """Borra lo que sembró ESTE archivo, sin apoyarse en una base virgen.

    Las funciones de cheques commitean, así que lo de la corrida anterior sigue
    ahí. Lo que no se puede hacer es borrar "todo lo que se parezca": el saldo del
    banco es una cadena compartida y sacarle una fila del medio a otro archivo lo
    deja con el saldo partido (ver la nota de test_cheques_state_machine_edge.py).
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
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s", (f"%{CLI}%",))
    if ids:
        # Borrar del medio deja a las filas de abajo con el saldo del estado
        # viejo: re-encadenar es lo que hace la app al eliminar un movimiento.
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


# ── 1. Depositar cartera (Z → B) → crea DE ──────────────────────────────
@pytest.mark.db
def test_1_depositar_Z_a_B_crea_movimiento_banco(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0001", importe=500, stat="Z")
    conn.commit()
    from modules.cheques import queries as chq

    chq.depositar_lote(ids_cheques=[id_ch], no_banco=1, usuario="test")
    assert _stat(conn, id_ch) == "B"
    movs = _banco_movs(conn, id_ch, "T0001", "DE")
    assert len(movs) == 1 and abs(float(movs[0][0]) - 500) < 0.01


# ── 2. Marcar devuelto directo (Z → 1) ──────────────────────────────────
@pytest.mark.db
def test_2_marcar_devuelto_Z_a_1(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0002", stat="Z")
    conn.commit()
    from modules.cheques import queries as chq

    chq.transicionar_stat(id_ch, stat_destino="1", usuario="test")
    assert _stat(conn, id_ch) == "1"


# ── 3. Re-depositar un devuelto individual (1 → V) → crea DE ────────────
@pytest.mark.db
def test_3_redepositar_1_a_V_crea_movimiento_banco(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0003", importe=750, stat="1")
    conn.commit()
    from modules.cheques import queries as chq

    chq.transicionar_stat(id_ch, stat_destino="V", usuario="test")
    assert _stat(conn, id_ch) == "V"
    movs = _banco_movs(conn, id_ch, "T0003", "DE")
    assert len(movs) == 1 and abs(float(movs[0][0]) - 750) < 0.01


# ── 4. Depositar lote un devuelto (1 → V) ──────────────────────────────
@pytest.mark.db
def test_4_depositar_lote_devuelto_da_V(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0004", stat="1")
    conn.commit()
    from modules.cheques import queries as chq

    chq.depositar_lote(ids_cheques=[id_ch], no_banco=1, usuario="test")
    assert _stat(conn, id_ch) == "V"


# ── 5. Rebote real (B → 1) → ND en banco, NO reabre la factura ─────────
@pytest.mark.db
def test_5_rebote_real_no_reabre_factura(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0005", importe=1000, stat="Z")
    id_fact = _seed_factura_aplicada(
        conn, codigo_cli="USR", numf=500005, importe=1000, id_cheque=id_ch, aplicado=1000
    )
    conn.commit()
    from modules.cheques import queries as chq

    # El usuario primero DEPOSITA el cheque (crea el DE en el banco), y despues rebota.
    chq.depositar_lote(ids_cheques=[id_ch], no_banco=1, usuario="test")
    assert _stat(conn, id_ch) == "B"
    chq.reversar(id_cheque=id_ch, motivo="rebote banco", usuario="test")
    assert _stat(conn, id_ch) == "1"  # vuelve a cartera como devuelto
    # ND en el banco (documento ND, descuenta)
    nd = _banco_movs(conn, id_ch, "T0005", "ND")
    assert len(nd) >= 1
    # Factura NO reabierta: abono/saldo/stat IGUAL que antes.
    abono, saldo, stat = _factura(conn, id_fact)
    assert abs(float(abono) - 1000) < 0.01 and float(saldo) <= 0.01 and stat == "T"
    # La aplicación (chequesxfact) SIGUE viva.
    assert _chequesxfact_count(conn, id_ch) == 1


# ── 6. Anular por error de carga (Z → X) → SÍ reabre la factura ────────
@pytest.mark.db
def test_6_anular_administrativo_reabre_factura(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0006", importe=1000, stat="Z")
    id_fact = _seed_factura_aplicada(
        conn, codigo_cli="USR", numf=500006, importe=1000, id_cheque=id_ch, aplicado=1000
    )
    conn.commit()
    from modules.cheques import queries as chq

    res = chq.reversar(id_cheque=id_ch, motivo="error carga", usuario="test")
    assert res["es_rebote_real"] is False
    assert _stat(conn, id_ch) == "X"
    # Factura REABIERTA: abono baja a 0, saldo vuelve al importe, stat Z.
    abono, saldo, stat = _factura(conn, id_fact)
    assert abs(float(abono)) < 0.01 and abs(float(saldo) - 1000) < 0.01 and stat == "Z"
    # La aplicación (chequesxfact) fue BORRADA.
    assert _chequesxfact_count(conn, id_ch) == 0


# ── 7. Postergar (Z → P) ───────────────────────────────────────────────
@pytest.mark.db
def test_7_postergar_Z_a_P(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0007", stat="Z")
    conn.commit()
    from modules.cheques import queries as chq

    chq.transicionar_stat(id_ch, stat_destino="P", usuario="test", nueva_fechad=HOY + timedelta(days=10))
    assert _stat(conn, id_ch) == "P"


# ── 8. Cobrar en efectivo (Z → C) → movimiento de caja ─────────────────
@pytest.mark.db
def test_8_cobrar_efectivo_Z_a_C(setup):
    conn = setup
    id_ch = _seed_cheque(conn, codigo_cli="USR", no_cheque="T0008", importe=300, stat="Z")
    conn.commit()
    from modules.cheques import queries as chq

    chq.transicionar_stat(id_ch, stat_destino="C", usuario="test")
    assert _stat(conn, id_ch) == "C"


# ── La huella en mov_doble (07/08/2026) ─────────────────────────────────────

def test_cada_transicion_deja_su_huella_en_mov_doble(monkeypatch):
    """🚨 `transicionar_stat` mueve plata en el BANCO —deposita, compensa un
    rebote, descuenta un devuelto— y no dejaba ninguna huella: esos
    movimientos no salían en /historial, no se podían revertir con el ↺, y en
    la traza el banco aparecía sin hecho que lo explicara. El depósito en LOTE
    sí la deja, así que el mismo cheque depositado de dos maneras distintas se
    contaba distinto.
    """
    from modules.cheques import queries as chq

    assert chq.TIPO_MD_TRANSICION["B"] == "cheque_depositado"
    assert chq.TIPO_MD_TRANSICION["V"] == "cheque_depositado"
    assert chq.TIPO_MD_TRANSICION["I"] == "cheque_depositado"
    assert chq.TIPO_MD_TRANSICION["C"] == "cheque_efectivo_to_caja"
    assert chq.TIPO_MD_TRANSICION["9"] == "cheque_rebotado"
    for d in ("1", "2", "3"):
        assert chq.TIPO_MD_TRANSICION[d] == "cheque_devuelto"
    # Un salto que no mueve plata igual deja huella: contesta quién lo pasó a
    # anulado y cuándo.
    assert chq.TIPO_MD_TRANSICION.get("X") is None
    assert chq.TIPO_MD_TRANSICION_OTRO == "cheque_stat_cambio"


def test_los_tipos_nuevos_tienen_nombre_en_el_historial_y_en_la_traza():
    """Un tipo sin entrada cae al fallback `tipo.replace('_',' ')`, que produce
    la cadena más larga posible en la columna más angosta."""
    from modules.cheques import queries as chq
    from modules.historial.queries import TIPOS_CORTO, TIPOS_LABEL

    for t in set(chq.TIPO_MD_TRANSICION.values()) | {chq.TIPO_MD_TRANSICION_OTRO}:
        assert t in TIPOS_LABEL, t
        assert t in TIPOS_CORTO, t
