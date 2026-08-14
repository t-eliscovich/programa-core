"""El cobro en EFECTIVO de un cheque partía la cadena de saldos de la caja.

⭐ EL BUG. `cheques.queries.crear()` con `no_banco = 99` (efectivo) metía la
fila de caja con `saldo = NULL` y se apoyaba en el trigger `trg_caja_set_saldo`
(mig 0022). El trigger encadena bien la fila NUEVA —toma el previo por
(fecha, id_caja)— pero es un BEFORE INSERT de una fila sola: no re-encadena las
que quedan DEBAJO, cuyo saldo se calculó sobre un estado que esta fila acaba de
cambiar, y no pasa por `assert_cadena_intacta`. Un cobro en efectivo con fecha
ATRASADA —que es lo normal cuando se pone al día la caja— dejaba la cadena
partida en dos tramos, cada uno coherente por su lado, que es la forma en que
este bug NO SE VE.

Es el mismo bug que en bancos costó los 155.187,31 del 03/08
[[project_2026_08_03_utilidad_37k]] y el que se cerró unas horas antes en
`/caja/nuevo` (commit abdd732). El arreglo es el mismo y a propósito: la fila
la escribe `caja_helpers.insert_movimiento_caja`, que trae las tres piezas
—saldo previo por (fecha, id_caja), re-encadenado de lo de abajo y el candado—
y ya es por donde entran la anulación por error de carga, el reverso y la
migración de depósito directo de este mismo archivo. Una segunda cuenta del
saldo era la tentación: la caja ya tuvo cuatro definiciones del signo
conviviendo y una estaba mal. [[feedback_espejo_clasificador_compartido]]

Los tests son `-m db` porque lo que se prueba es cómo quedan las FILAS de una
cadena real después del cobro, no qué SQL se emitió.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

PREFIJO = "CHQEFECAJATEST"
CLI = "ZCE"          # cliente de prueba, sólo de este archivo
CONCEPTO_COBRO = f"CH.{CLI}"   # lo que escribe `crear()` (paridad PASOCAJA)
HOY = date.today()


def _d(n: int) -> date:
    return HOY - timedelta(days=n)


def _limpiar(conn) -> None:
    """Borra SÓLO lo de este archivo, y RE-ENCADENA lo que queda.

    La caja es UNA sola cadena compartida por toda la suite: borrar filas del
    medio sin re-encadenar deja corridos los saldos de abajo y el próximo
    archivo que cargue algo se come el candado. Es la misma disciplina que
    exigen `test_candado_cadena_caja_2026_08_14` y el test hermano del alta de
    /caja/nuevo. [[feedback_verificar_como_corre_el_ci]]
    """
    import caja_helpers
    import db

    cur = conn.cursor()
    cur.execute(
        """
        SELECT MIN(fecha), COALESCE(ARRAY_AGG(id_caja), '{}')
          FROM scintela.caja
         WHERE concepto LIKE %s OR concepto = %s
        """,
        (f"{PREFIJO}%", CONCEPTO_COBRO),
    )
    desde, ids_caja = cur.fetchone()
    cur.execute("SELECT COALESCE(ARRAY_AGG(id_cheque), '{}') "
                "  FROM scintela.cheque WHERE codigo_cli = %s", (CLI,))
    (ids_cheque,) = cur.fetchone()
    # El historial unificado apunta por (tabla, id): dejar mov_doble colgando
    # de filas borradas rompe /informes/traza para todos los demás tests.
    for tabla, ids in (("caja", ids_caja), ("cheque", ids_cheque)):
        if ids:
            cur.execute(
                "DELETE FROM scintela.mov_doble "
                " WHERE (origen_table = %s AND origen_id = ANY(%s)) "
                "    OR (destino_table = %s AND destino_id = ANY(%s))",
                (tabla, list(ids), tabla, list(ids)),
            )
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s OR concepto = %s",
                (f"{PREFIJO}%", CONCEPTO_COBRO))
    if ids_cheque:
        cur.execute("DELETE FROM scintela.chequesxfact WHERE id_cheque = ANY(%s)",
                    (list(ids_cheque),))
        cur.execute("DELETE FROM scintela.cheque WHERE id_cheque = ANY(%s)",
                    (list(ids_cheque),))
    conn.commit()
    if desde is not None:
        with db.tx() as c2:
            caja_helpers.recompute_saldos_desde(c2, ancla_fecha=desde)


@pytest.fixture
def caja_limpia(real_db_conn, migrated_db):
    cur = real_db_conn.cursor()
    cur.execute(
        """
        INSERT INTO scintela.cliente
               (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo)
        VALUES (DEFAULT, %s, 'Cliente cobro efectivo test', 'N', 30, 10000, TRUE)
        ON CONFLICT (codigo_cli) DO NOTHING
        """,
        (CLI,),
    )
    real_db_conn.commit()
    _limpiar(real_db_conn)
    yield real_db_conn
    _limpiar(real_db_conn)


def _saldo_de(conn, id_caja: int) -> float:
    cur = conn.cursor()
    cur.execute("SELECT saldo FROM scintela.caja WHERE id_caja = %s", (id_caja,))
    return float(cur.fetchone()[0])


def _sembrar(fecha, tipo, importe, sufijo) -> int:
    """Una fila de caja cualquiera, por el helper (que es la vía sana)."""
    import caja_helpers
    import db
    with db.tx() as c:
        return caja_helpers.insert_movimiento_caja(
            c, fecha=fecha, tipo=tipo, importe=importe,
            concepto=f"{PREFIJO}-{sufijo}", usuario="test")["id_caja"]


def _cobrar_efectivo(fecha, importe, no_cheque="") -> dict:
    """El cobro en efectivo REAL — `crear()` es lo que corre /cheques/nuevo."""
    from modules.cheques import queries as q
    return q.crear(fecha=fecha, fechad=fecha, codigo_cli=CLI,
                   no_cheque=no_cheque, importe=importe, no_banco=99,
                   usuario="test")


def _caja_del_cheque(conn, id_cheque: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id_caja FROM scintela.caja WHERE id_cheque = %s",
                (id_cheque,))
    fila = cur.fetchone()
    assert fila, "el cobro en efectivo no generó fila de caja"
    return int(fila[0])


# ── EL TEST QUE LE DA SENTIDO A TODO ─────────────────────────────────────

@pytest.mark.db
def test_db_cobro_efectivo_con_fecha_atrasada_encadena_por_fecha(caja_limpia):
    """⭐ EL CASO. Cobrar hoy, en efectivo, un cheque del miércoles pasado.

    (a) su saldo tiene que salir de la fila que lo PRECEDE POR FECHA, no de la
        última cargada;
    (b) las filas posteriores por fecha tienen que quedar re-encadenadas;
    (c) `contar_quiebres` tiene que devolver [].

    Con el INSERT viejo (saldo NULL + trigger) (a) SÍ salía bien —el trigger
    también busca por (fecha, id_caja)— pero (b) no pasaba nunca: las de abajo
    se quedaban con el saldo del estado anterior al cobro. Los números están
    elegidos para que el corrimiento no se pueda confundir con nada.
    """
    import caja_helpers
    conn = caja_limpia

    id1 = _sembrar(_d(5), "E", 1000, "1")
    id2 = _sembrar(_d(3), "E", 300, "2")
    id3 = _sembrar(_d(2), "S", 200, "3")
    s1, s2, s3 = (_saldo_de(conn, i) for i in (id1, id2, id3))
    assert (s2, s3) == (s1 + 300, s1 + 100), "la siembra ya tiene que encadenar"

    # El cobro atrasado: cae ENTRE la 1 y la 2.
    res = _cobrar_efectivo(_d(4), 500)
    id_caja = _caja_del_cheque(conn, res["id_cheque"])

    # (a) el saldo salió de la fila que la precede POR FECHA (s1), no de la
    #     última insertada (s3). s3 = s1 + 100, así que las dos cuentas dan
    #     distinto y el test puede distinguirlas.
    s_cobro = _saldo_de(conn, id_caja)
    assert s_cobro == pytest.approx(s1 + 500), (
        "el saldo del cobro sale de la fila que lo precede POR FECHA")
    assert s_cobro != pytest.approx(s3 + 500), "eso sería el orden de carga"

    # (b) las de abajo se corrieron los 500 que entraron arriba. ESTO es lo
    #     que el trigger no hacía: sin re-encadenar, s2 y s3 quedaban iguales.
    assert _saldo_de(conn, id2) == pytest.approx(s2 + 500), (
        "la fila de abajo quedó con el saldo de antes del cobro — cadena partida")
    assert _saldo_de(conn, id3) == pytest.approx(s3 + 500)

    # (c) y la cadena quedó entera.
    assert caja_helpers.contar_quiebres(desde_fecha=_d(6)) == []


@pytest.mark.db
def test_db_la_devolucion_en_efectivo_atrasada_tambien_re_encadena(caja_limpia):
    """El efectivo NEGATIVO es una SALIDA ('S'), y resta también hacia abajo.

    Regresión doble: que el signo siga viviendo en el `tipo` (TMT 2026-07-30,
    el trigger usa ABS(importe)) y que el re-encadenado lo respete. Si el
    helper hubiera tomado el importe crudo o el tipo por otro lado, esta
    devolución sumaría plata que salió de la lata.
    """
    import caja_helpers
    conn = caja_limpia

    id1 = _sembrar(_d(5), "E", 5000, "dev1")
    id2 = _sembrar(_d(2), "E", 700, "dev2")
    s1, s2 = _saldo_de(conn, id1), _saldo_de(conn, id2)

    res = _cobrar_efectivo(_d(4), -1234.56, no_cheque="")
    id_caja = _caja_del_cheque(conn, res["id_cheque"])

    cur = conn.cursor()
    cur.execute("SELECT tipo, importe, concepto FROM scintela.caja WHERE id_caja = %s",
                (id_caja,))
    tipo, importe, concepto = cur.fetchone()
    assert tipo.strip() == "S", "una devolución es SALIDA de caja"
    assert float(importe) == pytest.approx(1234.56), "el importe va SIEMPRE positivo"
    assert concepto.strip().startswith("DEV."), "se lee qué es sin abrir el cheque"

    assert _saldo_de(conn, id_caja) == pytest.approx(s1 - 1234.56)
    assert _saldo_de(conn, id2) == pytest.approx(s2 - 1234.56), (
        "la devolución atrasada tiene que restar también en lo que va debajo")
    assert caja_helpers.contar_quiebres(desde_fecha=_d(6)) == []


@pytest.mark.db
def test_db_el_cobro_del_dia_sigue_saliendo_igual_que_siempre(caja_limpia):
    """El caso de todos los días no puede haberse movido.

    Un arreglo que sólo mejorara la carga atrasada y dejara el cobro normal
    colgando de cualquier fila no serviría de nada. "El final del libro" es el
    final por (fecha, id_caja) —lo que muestra el arqueo de /caja—, que NO es
    lo mismo que la última fila cargada.
    """
    import caja_helpers
    conn = caja_limpia

    _sembrar(_d(1), "E", 800, "hoy1")
    previo = caja_helpers.saldo_actual()

    res = _cobrar_efectivo(HOY, 250)
    id_caja = _caja_del_cheque(conn, res["id_cheque"])

    assert _saldo_de(conn, id_caja) == pytest.approx(previo + 250)
    assert caja_helpers.contar_quiebres(desde_fecha=_d(2)) == []


@pytest.mark.db
def test_db_el_saldo_no_queda_nunca_en_NULL(caja_limpia):
    """Nadie más tiene que llenar ese campo después.

    Con `saldo = NULL` la fila dependía de que el trigger estuviera puesto en
    ESA base. `informes.queries.salcaj()` y el arqueo leen la columna: una fila
    en NULL es plata que no aparece. Ahora el saldo se estampa desde Python y
    el trigger hace early-return.
    """
    conn = caja_limpia
    _sembrar(_d(3), "E", 100, "null1")
    res = _cobrar_efectivo(_d(2), 42.42)
    cur = conn.cursor()
    cur.execute("SELECT saldo FROM scintela.caja WHERE id_cheque = %s",
                (res["id_cheque"],))
    (saldo,) = cur.fetchone()
    assert saldo is not None, "el saldo tiene que venir calculado, no NULL"


@pytest.mark.db
def test_db_la_fila_de_caja_cae_en_la_misma_transaccion_que_el_cheque(caja_limpia):
    """Si el cobro se rollbackea, la plata NO queda en la caja.

    ⭐ La fila de caja se escribe con el `conn` del `db.tx()` del alta, no en
    una transacción aparte. Es lo que hace que un cobro que revienta a mitad
    de camino no deje plata en la caja sin cheque que la explique — y también
    lo que hace que el candado de la cadena sirva de algo: si el candado
    reventara pero la caja ya estuviera commiteada, el candado sólo avisaría.
    """
    import caja_helpers
    import db
    conn = caja_limpia

    _sembrar(_d(3), "E", 900, "tx1")
    antes = caja_helpers.saldo_actual()

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.caja")
    (n_antes,) = cur.fetchone()

    from modules.cheques import queries as q
    with pytest.raises(RuntimeError, match="BOOM"), db.tx() as c:
        q.crear(fecha=_d(2), fechad=_d(2), codigo_cli=CLI, no_cheque="",
                importe=333.0, no_banco=99, usuario="test", conn=c)
        raise RuntimeError("BOOM — algo posterior del cobro falla")

    conn.rollback()   # que la sesión del test vea el estado commiteado
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.caja")
    (n_despues,) = cur.fetchone()
    assert n_despues == n_antes, "la fila de caja sobrevivió al rollback del cobro"
    assert caja_helpers.saldo_actual() == pytest.approx(antes)
