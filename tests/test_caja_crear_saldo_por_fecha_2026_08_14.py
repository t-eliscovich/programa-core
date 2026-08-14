"""El alta de /caja/nuevo tomaba el saldo de la ÚLTIMA FILA CARGADA. TMT 2026-08-14.

⭐ EL BUG. `caja.queries.crear()` —el alta de /caja/nuevo, que es *la* pantalla
de caja— calculaba el saldo anterior con `ORDER BY id_caja DESC`, o sea por
orden de INSERCIÓN. Pero la caja la lee por `(fecha, id_caja)` todo el resto
del sistema: `caja.queries.saldo_actual()`, `informes.queries.salcaj()`,
`caja_helpers.recompute_saldos_desde` y el candado de la cadena. Una carga con
fecha atrasada se estampaba entonces con el saldo de una fila que va DESPUÉS de
ella en el tiempo, y las que le quedaban abajo conservaban el saldo del estado
viejo: la cadena partida en dos tramos, cada uno coherente por su lado, que es
la forma en que este bug NO SE VE.

Es el mismo bug que en bancos costó los 155.187,31 del 03/08
[[project_2026_08_03_utilidad_37k]]. El candado que se puso el 13/08 (commit
83f6adf) no lo agarraba: vive en `caja_helpers` y esta pantalla no pasaba por
ahí.

El arreglo no reescribe el orden acá: hace que `crear()` escriba por
`caja_helpers.insert_movimiento_caja`, que ya encadena por (fecha, id_caja),
re-encadena lo que queda debajo y cierra por `assert_cadena_intacta`.

Los tests son `-m db` a propósito: lo que se está probando es cómo quedan las
FILAS de una cadena real después del alta, no qué SQL se emitió.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIJO = "CAJAFECHATEST"
HOY = date.today()


def _d(n: int) -> date:
    return HOY - timedelta(days=n)


def _limpiar(conn) -> None:
    """Borra SÓLO lo que siembra este archivo, y RE-ENCADENA lo que queda.

    La caja es UNA sola cadena compartida por toda la suite: borrar filas del
    medio sin re-encadenar deja los saldos de abajo corridos y el próximo
    archivo que cargue algo se come el candado. Es la misma disciplina que
    exige `test_candado_cadena_caja_2026_08_14`.
    """
    import caja_helpers
    import db

    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(fecha), COALESCE(ARRAY_AGG(id_caja), '{}') "
        "  FROM scintela.caja WHERE concepto LIKE %s",
        (f"{PREFIJO}%",),
    )
    desde, ids = cur.fetchone()
    if ids:
        # El mov_doble que registra `crear()` quedaría apuntando a filas de
        # caja que ya no existen — el historial lo lee por (tabla, id).
        cur.execute(
            "DELETE FROM scintela.mov_doble "
            " WHERE origen_table = 'caja' AND origen_id = ANY(%s)",
            (list(ids),),
        )
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s",
                (f"{PREFIJO}%",))
    conn.commit()
    if desde is not None:
        with db.tx() as c2:
            caja_helpers.recompute_saldos_desde(c2, ancla_fecha=desde)


@pytest.fixture
def caja_limpia(real_db_conn, migrated_db):
    _limpiar(real_db_conn)
    yield real_db_conn
    _limpiar(real_db_conn)


def _saldo_de(conn, id_caja: int) -> float:
    cur = conn.cursor()
    cur.execute("SELECT saldo FROM scintela.caja WHERE id_caja = %s", (id_caja,))
    return float(cur.fetchone()[0])


def _alta(fecha, tipo, importe, sufijo):
    """Alta POR LA PANTALLA — `crear()` es lo que corre /caja/nuevo."""
    from modules.caja import queries as cq
    return cq.crear(fecha=fecha, tipo=tipo, importe=importe,
                    concepto=f"{PREFIJO}-{sufijo}", clave="TST",
                    usuario="test")


# ── EL TEST QUE LE DA SENTIDO A TODO ─────────────────────────────────────

@pytest.mark.db
def test_db_alta_con_fecha_atrasada_encadena_por_fecha_y_re_encadena_lo_de_abajo(
    caja_limpia,
):
    """⭐ EL CASO. Cargar hoy un movimiento del martes pasado.

    (a) su saldo tiene que salir de la fila que lo PRECEDE POR FECHA, no de la
        última que se cargó;
    (b) las filas posteriores por fecha tienen que quedar re-encadenadas;
    (c) la cadena tiene que quedar sin quiebres.

    Con el `ORDER BY id_caja DESC` viejo, (a) daba el saldo del movimiento del
    lunes de ESTA semana —que en el libro va DEBAJO del que estoy cargando— y
    (b) directamente no pasaba.
    """
    import caja_helpers
    conn = caja_limpia

    r1 = _alta(_d(5), "E", 1000, "1")
    r2 = _alta(_d(3), "E", 300, "2")
    r3 = _alta(_d(2), "S", 200, "3")

    s1 = _saldo_de(conn, r1["id_caja"])
    s2 = _saldo_de(conn, r2["id_caja"])
    s3 = _saldo_de(conn, r3["id_caja"])
    assert (s2, s3) == (s1 + 300, s1 + 100), "la siembra ya tiene que encadenar"

    # El alta atrasada: cae ENTRE la 1 y la 2.
    r_atrasada = _alta(_d(4), "E", 500, "atrasada")

    # (a) el saldo salió del que la precede POR FECHA (s1), no del último
    #     insertado (s3). Los números están elegidos para que las dos cuentas
    #     den distinto: s3 = s1 + 100, así que el bug daba s1 + 600.
    s_atrasada = _saldo_de(conn, r_atrasada["id_caja"])
    assert s_atrasada == pytest.approx(s1 + 500), (
        "el saldo de una carga atrasada sale de la fila que la precede POR "
        "FECHA — con el ORDER BY id_caja DESC salía de la última cargada")
    assert s_atrasada != pytest.approx(s3 + 500), "eso es el bug viejo, exacto"
    assert r_atrasada["saldo_nuevo"] == pytest.approx(s_atrasada), (
        "lo que devuelve la pantalla tiene que ser lo GUARDADO")

    # (b) las de abajo se corrieron los 500 que se metieron arriba.
    assert _saldo_de(conn, r2["id_caja"]) == pytest.approx(s2 + 500)
    assert _saldo_de(conn, r3["id_caja"]) == pytest.approx(s3 + 500)

    # (c) y la cadena quedó entera.
    assert caja_helpers.contar_quiebres(desde_fecha=_d(6)) == []


@pytest.mark.db
def test_db_el_alta_del_dia_sigue_saliendo_igual_que_siempre(caja_limpia):
    """El alta normal —la de hoy— encadena con el FINAL del libro.

    Es el otro lado del candado: un arreglo que sólo mejorara el caso raro y
    dejara el saldo del alta de todos los días colgando de cualquier fila no
    serviría de nada. El "final del libro" es el final por (fecha, id_caja),
    que es lo que muestra el arqueo de /caja — y NO es lo mismo que la última
    fila cargada: en la base de tests el id más alto no es el del día más
    reciente, así que con el SQL viejo este test también se cae.
    """
    import caja_helpers
    conn = caja_limpia

    _alta(_d(1), "E", 800, "hoy1")
    # El saldo del final de la cadena AHORA: la caja de la base de tests ya
    # tiene filas de hoy, así que el previo no es la fila que acabo de
    # sembrar. [[feedback_total_de_pantalla_no_es_de_la_pagina]]
    previo = caja_helpers.saldo_actual()
    r2 = _alta(HOY, "S", 250, "hoy2")

    assert _saldo_de(conn, r2["id_caja"]) == pytest.approx(previo - 250)
    assert r2["saldo_nuevo"] == pytest.approx(previo - 250)
    assert caja_helpers.contar_quiebres(desde_fecha=_d(2)) == []


# ── 'CB' — el tipo que el helper no aceptaba ─────────────────────────────

@pytest.mark.db
def test_db_el_cobro_con_cheque_cb_entra_y_SUMA(caja_limpia):
    """'CB' es plata que ENTRA, igual que 'E'.

    `crear()` acepta E/S/CB; `insert_movimiento_caja` sólo aceptaba E/S. Se
    resolvió abriendo la lista del helper, NO escribiendo el saldo por afuera:
    el signo de 'CB' ya estaba definido —y en +1— en `signo_tipo`, que es la
    misma regla del trigger `trg_caja_set_saldo` (mig 0022), de
    `saldo_actual()` y de `salcaj()`. Si el helper lo hubiera tratado como
    salida, cada cobro con cheque habría restado plata que sí entró.
    """
    import caja_helpers
    conn = caja_limpia

    r1 = _alta(_d(2), "E", 400, "cb-previo")
    s1 = _saldo_de(conn, r1["id_caja"])

    r_cb = _alta(_d(1), "CB", 150, "cb")

    cur = conn.cursor()
    cur.execute("SELECT tipo, saldo FROM scintela.caja WHERE id_caja = %s",
                (r_cb["id_caja"],))
    tipo, saldo = cur.fetchone()
    assert tipo.strip() == "CB", "el tipo se guarda tal cual, no se convierte en E"
    assert float(saldo) == pytest.approx(s1 + 150), "'CB' SUMA"
    assert caja_helpers.contar_quiebres(desde_fecha=_d(3)) == []


@pytest.mark.db
def test_db_un_tipo_que_no_existe_sigue_rebotando(caja_limpia):
    """Abrir la lista para 'CB' no la abrió para cualquier cosa."""
    import caja_helpers
    import db
    with pytest.raises(ValueError, match="tipo debe ser"), db.tx() as c:
        caja_helpers.insert_movimiento_caja(
            c, fecha=HOY, tipo="X", importe=10,
            concepto=f"{PREFIJO}-tipo-raro", usuario="test")


# ── la regla de "no dejar la caja en negativo" ───────────────────────────

@pytest.mark.db
def test_db_una_salida_que_deja_la_caja_de_HOY_en_rojo_no_entra(caja_limpia):
    """Plata que no está: se frena siempre, con la fecha que sea.

    El saldo de HOY es el que se cuenta en el arqueo y el que publican
    `saldo_actual()` y `salcaj()`. Que la salida venga con fecha atrasada no
    la habilita: al re-encadenar, el rojo llega hasta hoy igual.
    """
    from modules.caja import queries as cq
    conn = caja_limpia

    _alta(_d(3), "E", 100, "neg-previo")
    hoy_actual = cq.saldo_actual()

    with pytest.raises(ValueError) as e:
        _alta(_d(2), "S", hoy_actual + 1000, "neg-no-entra")
    assert "en negativo" in str(e.value)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.caja WHERE concepto = %s",
                (f"{PREFIJO}-neg-no-entra",))
    assert cur.fetchone()[0] == 0, "el raise vive adentro del db.tx(): rollback"
    assert cq.saldo_actual() == pytest.approx(hoy_actual)


@pytest.mark.db
def test_db_un_negativo_SOLO_INTERMEDIO_sí_entra(caja_limpia):
    """⭐ LA DECISIÓN, escrita como test.

    Al pasar el saldo a orden (fecha, id_caja), "no dejar la caja en negativo"
    dejó de ser un solo número: para una carga atrasada, el saldo EN LA FECHA
    del movimiento y el saldo DE HOY pueden dar de signos opuestos.

    Se chequea el de HOY, y sólo el de hoy. El intermedio no se frena porque
    adentro de un mismo día las filas se ordenan por id_caja —orden de CARGA,
    no de reloj—: un negativo intermedio puede ser puro artefacto de en qué
    orden se tipearon dos movimientos. Frenarlo sería bloquear la corrección
    de una historia que ya pasó, para protegernos de algo que no ocurrió.

    Acá: el gasto del lunes deja ESE punto de la cadena en −1.000, pero el
    depósito del martes ya entró y la caja de hoy está en verde. La carga
    entra, el saldo intermedio queda negativo a la vista en la lista, y la
    cadena sigue entera.
    """
    import caja_helpers
    from modules.caja import queries as cq
    conn = caja_limpia

    r1 = _alta(_d(5), "E", 1000, "int-1")
    s1 = _saldo_de(conn, r1["id_caja"])
    _alta(_d(2), "E", 1_000_000, "int-2")

    # Una salida atrasada MÁS GRANDE que todo lo que había hasta ese día.
    r_int = _alta(_d(4), "S", s1 + 1000, "int-negativo")

    assert _saldo_de(conn, r_int["id_caja"]) == pytest.approx(-1000), (
        "el saldo EN ESA FECHA queda en rojo — y se guarda igual")
    assert cq.saldo_actual() > 0, "pero la caja de hoy está en verde"
    assert caja_helpers.contar_quiebres(desde_fecha=_d(6)) == []
