"""Los dos re-encadenados de caja de `modules/bancos/queries.py`. TMT 2026-08-14.

⭐ POR QUÉ EXISTE ESTE ARCHIVO. La caja es una CADENA: el `saldo` de cada fila
es el acumulado hasta ahí, así que borrar una fila del medio corre el saldo de
todas las de abajo. `bancos/queries.py` borra filas de caja en dos lugares —el
reverso de un movimiento simple y el deshacer-reverso de un cheque emitido— y
los dos re-encadenaban de una forma que no protegía nada:

1. `reversar_movimiento_simple` envolvía el recompute en un
   `try/except Exception: pass`. Desde el commit 83f6adf el recompute cierra
   con `assert_cadena_intacta`, o sea que ese `pass` se comía JUSTO el error
   que el candado existe para dar: el candado estaba puesto y en ese punto no
   podía frenar nada ni dejar rastro. Un `except` mudo que se traga al vigía.

2. `deshacer_reverso_cheque_emitido` anclaba el walk en
   `MIN(id_caja) > cid` — o sea por orden de CARGA, cuando el walk camina por
   `(fecha, id_caja)`. Las filas de fecha intermedia con id menor al ancla
   quedaban afuera y conservaban el saldo viejo; y si la fila compensatoria
   era la última cargada (el caso típico: el reverso se hace después), el
   `MIN(...)` daba NULL y no se re-encadenaba NADA.

Los dos son la misma trampa que en bancos costó los **155.187,31 del
03/08/2026** [[project_2026_08_03_utilidad_37k]], y la misma que se cerró esta
semana en `/caja/nuevo` (abdd732) y en `bank_helpers` (b10100f).

Son tests `-m db` a propósito: lo que se prueba es cómo quedan las FILAS de
una cadena real después de la operación, no qué SQL se emitió.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIJO = "REVCAJATEST"
NO_BANCO = 66          # BANECUADOR: está en el catálogo y no tiene movimientos
HOY = date.today()


def _d(n: int) -> date:
    return HOY - timedelta(days=n)


# ── limpieza ─────────────────────────────────────────────────────────────

def _limpiar(conn) -> None:
    """Borra SÓLO lo que siembra este archivo, y RE-ENCADENA la caja.

    La caja es UNA sola cadena compartida por toda la suite: borrar filas del
    medio sin re-encadenar deja los saldos de abajo corridos y el próximo
    archivo que cargue algo se come el candado. Misma disciplina que
    `test_candado_cadena_caja_2026_08_14` y `test_caja_crear_saldo_por_fecha`.
    """
    import caja_helpers
    import db

    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(fecha), COALESCE(ARRAY_AGG(id_caja), '{}') "
        "  FROM scintela.caja WHERE concepto LIKE %s",
        (f"%{PREFIJO}%",),
    )
    desde, ids_caja = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(ARRAY_AGG(id_transaccion), '{}') "
        "  FROM scintela.transacciones_bancarias WHERE no_banco = %s",
        (NO_BANCO,),
    )
    (ids_tx,) = cur.fetchone()
    ids_caja, ids_tx = list(ids_caja or []), list(ids_tx or [])
    # Los mov_doble a barrer son los que cuelgan de MIS filas — incluidos los
    # que crea el propio reverso, que no llevan el prefijo en el concepto.
    cur.execute(
        "SELECT COALESCE(ARRAY_AGG(id_mov_doble), '{}') FROM scintela.mov_doble "
        " WHERE (origen_table='caja' AND origen_id = ANY(%s)) "
        "    OR (destino_table='caja' AND destino_id = ANY(%s)) "
        "    OR (origen_table='transacciones_bancarias' AND origen_id = ANY(%s)) "
        "    OR (destino_table='transacciones_bancarias' AND destino_id = ANY(%s))",
        (ids_caja, ids_caja, ids_tx, ids_tx),
    )
    (ids_md,) = cur.fetchone()
    ids_md = list(ids_md or [])
    if ids_md:
        # `mov_doble` se apunta a sí misma (id_original / id_reverso): hay que
        # soltar los links ANTES de borrar o la FK rebota.
        cur.execute("UPDATE scintela.mov_doble SET id_original=NULL "
                    " WHERE id_original = ANY(%s)", (ids_md,))
        cur.execute("UPDATE scintela.mov_doble SET id_reverso=NULL "
                    " WHERE id_reverso = ANY(%s)", (ids_md,))
        cur.execute("DELETE FROM scintela.mov_doble WHERE id_mov_doble = ANY(%s)",
                    (ids_md,))
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s", (f"%{PREFIJO}%",))
    cur.execute("DELETE FROM scintela.transacciones_bancarias WHERE no_banco = %s",
                (NO_BANCO,))
    conn.commit()
    if desde is not None:
        with db.tx() as c2:
            caja_helpers.recompute_saldos_desde(c2, ancla_fecha=desde)


@pytest.fixture
def entorno(real_db_conn, migrated_db):
    _limpiar(real_db_conn)
    yield real_db_conn
    _limpiar(real_db_conn)


# ── siembra ──────────────────────────────────────────────────────────────

def _saldo_de(conn, id_caja: int):
    cur = conn.cursor()
    cur.execute("SELECT saldo FROM scintela.caja WHERE id_caja = %s", (id_caja,))
    r = cur.fetchone()
    return None if r is None else float(r[0])


def _caja(conn_tx, fecha, tipo, importe, sufijo):
    import caja_helpers
    return caja_helpers.insert_movimiento_caja(
        conn_tx, fecha=fecha, tipo=tipo, importe=importe,
        concepto=f"{PREFIJO}-{sufijo}", clave="TST", usuario="test",
    )["id_caja"]


def _banco(conn_tx, fecha, documento, importe, sufijo):
    import bank_helpers
    return bank_helpers.insert_movimiento_bancario(
        conn_tx, no_banco=NO_BANCO, no_cta=None, fecha=fecha,
        documento=documento, importe=importe,
        concepto=f"{PREFIJO}-{sufijo}", usuario="test",
    )["id_transaccion"]


def _sembrar_cadena_con_compensacion(tipo_comp="S", importe_comp=40):
    """Caja con una fila BACKDATED cargada AL FINAL — el caso del bug.

    Orden por (fecha, id_caja):  c1(d6) · comp(d5) · c2(d4) · c3(d2)
    Orden por id_caja (carga):   c1 · c2 · c3 · comp

    O sea: `comp` tiene el id MÁS ALTO y una fecha del MEDIO. Las filas c2 y c3
    van DEBAJO de ella en el libro pero tienen id MENOR — son exactamente las
    que el ancla por id dejaba afuera.
    """
    import db
    with db.tx() as conn:
        c1 = _caja(conn, _d(6), "E", 1000, "c1")
        c2 = _caja(conn, _d(4), "E", 300, "c2")
        c3 = _caja(conn, _d(2), "S", 200, "c3")
        comp = _caja(conn, _d(5), tipo_comp, importe_comp, "comp")
    return c1, c2, c3, comp


# ═════════════════════════════════════════════════════════════════════════
# (2) deshacer_reverso_cheque_emitido — el ancla por MIN(id_caja)
# ═════════════════════════════════════════════════════════════════════════

def _armar_reverso_de_cheque_con_caja(comp_id, importe=40):
    """Deja la base como la deja `reversar_cheque_emitido` de un cheque a caja.

    Es decir: el cheque original en el banco + su mov_doble marcado
    'reversado', la NC compensatoria en el banco, y el mov_doble del reverso
    con `side_effect_revertido = {'tipo': 'caja_compensada', ...}` apuntando a
    la fila de caja que hay que borrar al deshacerlo.
    """
    import db
    import mov_doble as _md
    with db.tx() as conn:
        tx_ch = _banco(conn, _d(6), "CH", importe, "cheque-original")
        id_nc = _banco(conn, _d(5), "NC", importe, "nc-del-reverso")
        md_orig = _md.registrar(
            conn=conn, tipo="cheque_emitido_caja",
            origen_table="transacciones_bancarias", origen_id=tx_ch,
            destino_table="caja", destino_id=comp_id,
            importe=importe, fecha=_d(6),
            concepto=f"{PREFIJO}-cheque a caja", usuario="test",
        )
        md_rev = _md.registrar(
            conn=conn, tipo="reverso_cheque_emitido",
            origen_table="transacciones_bancarias", origen_id=id_nc,
            destino_table="transacciones_bancarias", destino_id=id_nc,
            importe=importe, fecha=_d(5),
            concepto=f"{PREFIJO}-reverso", usuario="test",
            metadata={"side_effect_revertido": {
                "tipo": "caja_compensada", "id_caja_compensacion": comp_id}},
            id_original=md_orig,
        )
        db.execute("UPDATE scintela.mov_doble SET estado='reverso' "
                   "WHERE id_mov_doble=%s", (md_rev,), conn=conn)
    return md_orig, md_rev


@pytest.mark.db
def test_db_deshacer_reverso_re_encadena_las_filas_de_id_menor(entorno):
    """⭐ EL CASO (2). La compensación de caja es la última CARGADA y su fecha
    es del MEDIO: las filas que le quedan debajo tienen id MENOR.

    Con el ancla por `MIN(id_caja) > cid` no había ancla siquiera (ninguna fila
    tiene id mayor), así que no se re-encadenaba nada y c2/c3 se quedaban con
    el saldo de un estado que ya no existe — la cadena partida justo donde el
    recompute creía haber pasado.
    """
    import caja_helpers
    from modules.bancos import queries as bq
    conn = entorno

    c1, c2, c3, comp = _sembrar_cadena_con_compensacion()
    s1, s2, s3 = (_saldo_de(conn, x) for x in (c1, c2, c3))
    assert caja_helpers.contar_quiebres(desde_fecha=_d(7)) == [], (
        "la siembra ya tiene que dejar la cadena entera")

    _md_orig, md_rev = _armar_reverso_de_cheque_con_caja(comp)
    out = bq.deshacer_reverso_cheque_emitido(
        id_mov_doble_reverso=md_rev, motivo="test", usuario="test")

    assert out["restaurado"]["tipo"] == "caja_compensacion_borrada"
    assert _saldo_de(conn, comp) is None, "la fila compensatoria se borró"

    # Sacar una SALIDA de 40 sube en 40 todo lo que queda debajo.
    assert _saldo_de(conn, c1) == pytest.approx(s1), "arriba del ancla no se toca"
    assert _saldo_de(conn, c2) == pytest.approx(s2 + 40), (
        "c2 tiene fecha POSTERIOR a la fila borrada pero id MENOR — con el "
        "ancla por MIN(id_caja) quedaba afuera del walk y conservaba el saldo "
        "viejo")
    assert _saldo_de(conn, c3) == pytest.approx(s3 + 40)
    assert caja_helpers.contar_quiebres(desde_fecha=_d(7)) == [], (
        "y la cadena quedó entera")


@pytest.mark.db
def test_db_deshacer_reverso_del_caso_normal_sigue_saliendo_igual(entorno):
    """El otro lado del mutante: el caso NORMAL —la compensación al final de lo
    que hay— tiene que seguir saliendo igual.

    Un arreglo que sólo mejorara el caso raro y rompiera el de todos los días
    no sirve. Acá no hay ninguna fila propia debajo del ancla, así que lo que
    se verifica es que no se toque nada de arriba y que la cadena siga entera.
    """
    import caja_helpers
    import db
    from modules.bancos import queries as bq
    conn = entorno

    with db.tx() as c:
        c1 = _caja(c, _d(6), "E", 1000, "solo-c1")
        comp = _caja(c, _d(1), "S", 40, "solo-comp")
    s1 = _saldo_de(conn, c1)

    _md_orig, md_rev = _armar_reverso_de_cheque_con_caja(comp)
    bq.deshacer_reverso_cheque_emitido(
        id_mov_doble_reverso=md_rev, motivo="test", usuario="test")

    assert _saldo_de(conn, comp) is None
    assert _saldo_de(conn, c1) == pytest.approx(s1)
    assert caja_helpers.contar_quiebres(desde_fecha=_d(7)) == []


# ═════════════════════════════════════════════════════════════════════════
# (1) reversar_movimiento_simple — el `except Exception: pass`
# ═════════════════════════════════════════════════════════════════════════

def _armar_deposito_con_caja(id_caja, importe=40):
    """Un mov_doble 'deposito' ACTIVO cuya metadata cuelga una fila de caja."""
    import db
    import mov_doble as _md
    with db.tx() as conn:
        tx = _banco(conn, _d(6), "DE", importe, "deposito-original")
        md = _md.registrar(
            conn=conn, tipo="deposito",
            origen_table="transacciones_bancarias", origen_id=tx,
            destino_table="caja", destino_id=id_caja,
            importe=importe, fecha=_d(6),
            concepto=f"{PREFIJO}-deposito", usuario="test",
            metadata={"id_caja": id_caja},
        )
    return tx, md


@pytest.mark.db
def test_db_el_candado_de_la_cadena_FRENA_el_reverso_entero(entorno, monkeypatch):
    """⭐ EL CASO (1). Si el re-encadenado dice que la caja quedaría partida,
    el reverso NO se guarda — ni el asiento del banco, ni la baja de la caja.

    El `CadenaCajaRotaError` se fuerza acá porque, con el ancla por fecha, el
    walk deja la cadena consistente por construcción: el candado es la red que
    tiene que estar puesta para el día en que algo NO previsto la parta. Lo que
    se prueba es que ese día el error SE NOTE en vez de morir en un `pass`.

    Con el `except Exception: pass` viejo, `reversar_movimiento_simple`
    terminaba OK: la compensación quedaba grabada en el banco, la fila de caja
    borrada, la cadena rota y nadie enterado.
    """
    import caja_helpers
    import db
    from modules.bancos import queries as bq
    conn = entorno

    with db.tx() as c:
        c1 = _caja(c, _d(6), "E", 1000, "cand-c1")
        cx = _caja(c, _d(3), "S", 40, "cand-cx")
    s1 = _saldo_de(conn, c1)
    _tx, md = _armar_deposito_con_caja(cx)

    def _revienta(*a, **k):
        raise caja_helpers.CadenaCajaRotaError("cadena partida (simulado)")

    monkeypatch.setattr(caja_helpers, "recompute_saldos_desde", _revienta)
    try:
        with pytest.raises(caja_helpers.CadenaCajaRotaError):
            bq.reversar_movimiento_simple(
                id_mov_doble=md, motivo="test", usuario="test")
    finally:
        # El `finally` no es adorno: sin él, el día que este test se ponga
        # rojo el parche queda puesto y se lleva puesto al teardown —y al
        # archivo de al lado. [[feedback_verificar_como_corre_el_ci]]
        monkeypatch.undo()

    # Rollback completo: el raise vive adentro del `db.tx()` del reverso.
    assert _saldo_de(conn, cx) is not None, "la fila de caja NO se borró"
    assert _saldo_de(conn, c1) == pytest.approx(s1)
    cur = conn.cursor()
    cur.execute("SELECT estado FROM scintela.mov_doble WHERE id_mov_doble=%s", (md,))
    assert cur.fetchone()[0] == "activo", "el original sigue activo"
    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias "
        " WHERE no_banco=%s AND documento='CH'", (NO_BANCO,))
    assert cur.fetchone()[0] == 0, (
        "la compensación bancaria del reverso tampoco quedó — si el `pass` "
        "vuelve, esta fila aparece y el test se pone rojo")
    assert caja_helpers.contar_quiebres(desde_fecha=_d(7)) == []


@pytest.mark.db
def test_db_reverso_de_deposito_re_encadena_por_fecha(entorno):
    """El reverso normal: la fila de caja que se va deja la cadena encadenada.

    Misma trampa que (2) —la fila de caja del depósito puede tener el id más
    alto y una fecha del medio— y mismo arreglo: el ancla es la FECHA de la
    fila borrada, leída ANTES del DELETE.
    """
    import caja_helpers
    from modules.bancos import queries as bq
    conn = entorno

    c1, c2, c3, comp = _sembrar_cadena_con_compensacion()
    s1, s2, s3 = (_saldo_de(conn, x) for x in (c1, c2, c3))
    _tx, md = _armar_deposito_con_caja(comp)

    bq.reversar_movimiento_simple(id_mov_doble=md, motivo="test", usuario="test")

    assert _saldo_de(conn, comp) is None
    assert _saldo_de(conn, c1) == pytest.approx(s1)
    assert _saldo_de(conn, c2) == pytest.approx(s2 + 40), (
        "fecha posterior, id menor: la fila que el ancla por id dejaba afuera")
    assert _saldo_de(conn, c3) == pytest.approx(s3 + 40)
    assert caja_helpers.contar_quiebres(desde_fecha=_d(7)) == []
