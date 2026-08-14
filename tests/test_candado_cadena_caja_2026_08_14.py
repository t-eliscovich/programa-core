"""El CANDADO de la cadena de saldos de la CAJA. TMT 2026-08-14.

⭐ POR QUÉ. `bank_helpers` tenía dos piezas: re-encadenar (`recompute_saldos_
desde`) y **no dejar commitear un write que parte la cadena**
(`assert_cadena_intacta`, 04/08). `caja_helpers` tenía sólo la primera. Hoy la
caja no muerde —ninguna pantalla publica plata leyendo su columna `saldo`;
`salcaj()` la deriva sumando— pero es la MISMA trampa esperando: una fila
borrada o insertada en el medio corre el saldo de todas las de abajo y nadie
se entera hasta que el arqueo no cierra.

Apareció arreglando la idempotencia de los tests de cheques: el equivalente
en banco ya había mordido dos veces (`test_cheques_transiciones_usuario`
borraba filas de caja sin re-encadenar, `test_cheques_ui_routes` dejaba la
fila del cobro en efectivo sin limpiar).

⭐ LA PRIMERA FILA — la decisión, que no se hereda del banco.
`bank_helpers.contar_quiebres` filtra `saldo_prev IS NOT NULL`: arranca en la
SEGUNDA fila, así que un saldo mal en la fila más vieja de un banco no lo ve
NUNCA. En banco eso es un hueco de verdad, porque existe una apertura afirmada
aparte (`scintela.banco_apertura`) contra la cual contrastarla.

En caja no hay ninguna tabla así, y la razón de fondo es más fuerte: **la
primera fila ES la declaración de la apertura**. `caja.queries.saldo_actual()`
y `salcaj()` calculan el opening como `saldo(primera fila) − delta(primera
fila)`. Contrastar esa fila contra un número sacado de ella misma da OK
siempre — un candado que no se puede activar se ve igual que uno que anda.

Por eso: se arranca en la segunda fila, como el banco, pero con la puerta
abierta. `contar_quiebres(apertura=...)` mete la primera fila al chequeo, y
está testeado acá abajo. El día que alguien afirme la apertura de la caja
(contra el arqueo físico, o una `caja_apertura` como la del banco), el hueco
se cierra pasándola — el mismo camino que ya usa
`bank_helpers.reencadenar_retro(apertura=...)`.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_bank_helpers import _FakeCajaDB  # noqa: E402


def _fila(id_caja, fecha, tipo, importe, saldo, concepto="x"):
    return {"id_caja": id_caja, "fecha": fecha, "tipo": tipo,
            "importe": importe, "saldo": saldo, "concepto": concepto}


def _caja_sana():
    """Tres días encadenados: 1.000 → 1.300 → 1.100."""
    return [
        _fila(1, date(2026, 8, 10), "E", 1000, 1000.0, "apertura del tramo"),
        _fila(2, date(2026, 8, 11), "E", 300, 1300.0, "cobro CHQ"),
        _fila(3, date(2026, 8, 12), "S", 200, 1100.0, "pago flete"),
    ]


def _instalar(monkeypatch, filas):
    import caja_helpers  # noqa: F401
    import db as db_mod
    fake = _FakeCajaDB(list(filas))
    fake.apply_to(monkeypatch, db_mod)
    return fake


# ── 1) el candado agarra el quiebre y NO deja guardar ────────────────────

def test_una_fila_borrada_del_dia_frena_la_carga_siguiente(monkeypatch):
    """⭐ EL CASO REAL: alguien borra un movimiento del día y no re-encadena.

    Las filas de abajo quedan con el saldo del estado viejo — la caja "tiene"
    300 que ya no están. Es literalmente lo que hacen las limpiezas de los
    tests de cheques (`DELETE ... WHERE concepto LIKE '%USR%'`) y lo que
    haría cualquiera arreglando una carga a mano. Antes de hoy la carga
    siguiente entraba igual y el arqueo arrastraba la diferencia sin fecha
    ni dueño; ahora frena en el acto y NO se guarda nada.

    Que el movimiento sea del MISMO día no es un detalle del test: la caja se
    carga y se corrige el mismo día, así que el agujero le queda debajo a la
    carga siguiente. Un quiebre de la semana pasada no la frena (ver
    `test_un_quiebre_viejo_no_bloquea_la_carga_de_hoy`).
    """
    import caja_helpers as cj

    filas = [
        _fila(1, date(2026, 8, 12), "E", 1000, 1000.0, "apertura del día"),
        _fila(2, date(2026, 8, 12), "E", 300, 1300.0, "cobro CHQ"),
        _fila(3, date(2026, 8, 12), "S", 200, 1100.0, "pago flete"),
    ]
    del filas[1]                     # se borró el cobro de 300
    fake = _instalar(monkeypatch, filas)

    assert len(cj.contar_quiebres()) == 1, "la última fila quedó colgada"

    with pytest.raises(cj.CadenaCajaRotaError) as e:
        cj.insert_movimiento_caja(
            conn=object(), fecha=date(2026, 8, 12), tipo="E",
            importe=50, concepto="cobro de hoy")

    assert "No se guardó nada" in str(e.value)
    assert "el saldo saltó 100.00 cuando el movimiento vale -200.00" in str(e.value), (
        "el mensaje tiene que decir el salto Y lo que la fila mueve, para que "
        "se pueda ir a mirar esa fila sin abrir la consola SQL")
    # y efectivamente no quedó nada escrito: el raise vive adentro del db.tx()
    # del caller, que hace rollback.
    assert [f["id_caja"] for f in fake.filas] == [1, 3, 4]


def test_con_la_cadena_sana_el_alta_pasa_sin_ruido(monkeypatch):
    """El otro lado del mutante: un candado que se activa SIEMPRE no sirve."""
    import caja_helpers as cj

    fake = _instalar(monkeypatch, _caja_sana())

    r = cj.insert_movimiento_caja(
        conn=object(), fecha=date(2026, 8, 13), tipo="E",
        importe=50, concepto="cobro de hoy")

    assert r["saldo_nuevo"] == 1150.0
    assert cj.contar_quiebres() == []
    assert len(fake.filas) == 4


def test_un_quiebre_viejo_no_bloquea_la_carga_de_hoy(monkeypatch):
    """El recorte por `desde_fecha`, igual que en banco.

    Sin él, el candado no se podría prender hasta terminar de planchar la
    historia — y mientras tanto la oficina no podría cargar.
    """
    import caja_helpers as cj

    filas = _caja_sana()
    filas[1]["saldo"] = 9999.0       # quiebre del 11/08, todavía sin limpiar
    filas[2]["saldo"] = 9799.0       # el resto encadena a partir de ahí
    _instalar(monkeypatch, filas)

    assert len(cj.contar_quiebres()) == 1
    r = cj.insert_movimiento_caja(
        conn=object(), fecha=date(2026, 8, 13), tipo="S",
        importe=99, concepto="pago de hoy")
    assert r["saldo_nuevo"] == 9700.0


# ── 2) una carga con fecha vieja NO es un quiebre: se re-encadena ────────

def test_backdated_reencadena_las_de_abajo_en_vez_de_frenar(monkeypatch):
    """Cargar una fecha vieja es legítimo y pasa TODOS los días.

    El candado tiene que proteger de un ledger partido, no de una fecha
    atrasada: por eso el insert re-encadena lo que quedó debajo antes de
    mirarse al espejo.
    """
    import caja_helpers as cj

    fake = _instalar(monkeypatch, _caja_sana())

    r = cj.insert_movimiento_caja(
        conn=object(), fecha=date(2026, 8, 11), tipo="E",
        importe=100, concepto="cobro que se traspapeló")

    por_id = {f["id_caja"]: f["saldo"] for f in fake.filas}
    assert por_id[1] == 1000.0, "lo anterior al ancla no se toca"
    assert por_id[2] == 1300.0
    assert por_id[4] == 1400.0, "la nueva va al final de su propio día"
    assert por_id[3] == 1200.0, "la del 12/08 se corrió los 100 que entraron"
    assert r["saldo_nuevo"] == 1400.0, "devuelve lo GUARDADO, no lo pre-walk"
    assert cj.contar_quiebres() == []


# ── 3) LA PRIMERA FILA: el hueco heredado, explícito ─────────────────────

def test_la_primera_fila_no_se_mira_sola_pero_si_contra_una_apertura(monkeypatch):
    """⭐ La decisión del 14/08, clavada en un test.

    Con la primera fila mal estampada y SIN apertura afirmada, el candado no
    la ve — igual que el del banco. No es un descuido: en caja el opening se
    LEE de esa misma fila, así que no hay con qué desmentirla.

    Pasando la apertura, la primera fila entra al chequeo como cualquier otra.
    """
    import caja_helpers as cj

    filas = _caja_sana()
    filas[0]["saldo"] = 4000.0       # la más vieja, mal
    filas[1]["saldo"] = 4300.0       # el resto encadena a partir de ella
    filas[2]["saldo"] = 4100.0
    _instalar(monkeypatch, filas)

    assert cj.contar_quiebres() == [], (
        "sin apertura afirmada la primera fila ES la apertura: nada la "
        "contradice")

    rotas = cj.contar_quiebres(apertura=0.0)
    assert [r["id_caja"] for r in rotas] == [1], (
        "afirmando que la caja arrancó en 0, la primera fila declara 3.000 "
        "que nunca entraron")
    assert rotas[0]["saldo_prev"] == 0.0


def test_con_apertura_la_primera_fila_bien_no_molesta(monkeypatch):
    """El espejo: la apertura correcta no inventa un quiebre."""
    import caja_helpers as cj

    _instalar(monkeypatch, _caja_sana())
    assert cj.contar_quiebres(apertura=0.0) == []


# ── 4) UNA sola regla de signo ───────────────────────────────────────────

def test_la_regla_de_signo_de_python_y_la_de_sql_son_la_misma():
    """Están escritas dos veces (Python y SQL). Que no se separen.

    La caja llegó a tener CUATRO definiciones del signo: `signo_tipo` (sólo E
    suma), `caja.queries.crear()` y el trigger de la mig 0022 (todo lo que no
    es S suma) y las dos lecturas de la plata. Con la vieja, cada fila 'CB'
    bien cargada era un quiebre.
    """
    import caja_helpers as cj

    sql = cj._sql_delta_firmado("c")
    assert "'S'" in sql and "-c.importe" in sql and "ELSE c.importe" in sql
    assert cj._delta_firmado("E", 100) == 100.0
    assert cj._delta_firmado("S", 100) == -100.0
    assert cj._delta_firmado("CB", 100) == 100.0, "el cobro con cheque SUMA"
    # importe crudo, sin abs(): las filas legacy del 17→30/04 vinieron
    # negativas y las dos lecturas de la plata las suman tal cual.
    assert cj._delta_firmado("S", -100) == 100.0


# ── 5) contra Postgres de verdad ─────────────────────────────────────────
#
# Los de arriba corren sobre el fake. El candado, sin embargo, vive en una
# window function de SQL: si la escribo mal, el fake no se entera. Estos
# tres lo ejercen contra la base — incluido el MUTANTE, que rompe un saldo
# a mano y confirma que lo agarra.

PREFIJO_CAJA = "CANDADO-CAJA-TEST"
HOY = date.today()


def _limpiar_caja(conn) -> None:
    """Borra SÓLO lo que siembra este archivo, y re-encadena lo que quede.

    Que es exactamente la disciplina que el candado va a exigirle a todo el
    mundo de ahora en más: si sacás una fila del medio, re-encadenás.
    """
    import caja_helpers
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(fecha) FROM scintela.caja WHERE concepto LIKE %s",
        (f"{PREFIJO_CAJA}%",),
    )
    desde = (cur.fetchone() or [None])[0]
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s",
                (f"{PREFIJO_CAJA}%",))
    conn.commit()
    if desde is not None:
        import db
        with db.tx() as c2:
            caja_helpers.recompute_saldos_desde(c2, ancla_fecha=desde)


@pytest.fixture
def caja_limpia(real_db_conn, migrated_db):
    _limpiar_caja(real_db_conn)
    yield real_db_conn
    _limpiar_caja(real_db_conn)


def _sembrar_tres(conn) -> list[int]:
    """Tres movimientos por la vía normal — la cadena queda sana."""
    import caja_helpers
    import db
    ids = []
    with db.tx() as c:
        for i, (tipo, imp) in enumerate([("E", 1000), ("E", 300), ("S", 200)]):
            r = caja_helpers.insert_movimiento_caja(
                c, fecha=HOY - timedelta(days=2 - i), tipo=tipo, importe=imp,
                concepto=f"{PREFIJO_CAJA} {i}", usuario="test")
            ids.append(r["id_caja"])
    return ids


@pytest.mark.db
def test_db_el_candado_agarra_un_saldo_roto_a_mano(caja_limpia):
    """⭐ EL MUTANTE. Rompo un saldo con SQL crudo y el candado tiene que
    frenar la carga siguiente — y no frenar nada cuando lo devuelvo a su
    lugar. Un candado que nunca se activa y uno que se activa siempre se ven
    igual desde el verde."""
    import caja_helpers
    import db
    conn = caja_limpia
    ids = _sembrar_tres(conn)

    assert caja_helpers.contar_quiebres(desde_fecha=HOY - timedelta(days=2)) == []

    cur = conn.cursor()
    cur.execute("UPDATE scintela.caja SET saldo = saldo + 111 WHERE id_caja = %s",
                (ids[1],))
    conn.commit()

    rotas = caja_helpers.contar_quiebres(desde_fecha=HOY - timedelta(days=2))
    # Un solo saldo mal deja DOS costuras: la fila que saltó y la que le
    # sigue, que ya no encadena con ella. Cuál es "la que sigue" depende de
    # lo que otros tests hayan dejado en la caja — es UNA sola cadena
    # compartida, no una por archivo.
    assert len(rotas) == 2
    assert rotas[0]["id_caja"] == ids[1]
    assert abs(float(rotas[0]["saldo"]) - float(rotas[0]["saldo_prev"])
               - float(rotas[0]["sgn"])) > 110

    with pytest.raises(caja_helpers.CadenaCajaRotaError), db.tx() as c:
        caja_helpers.insert_movimiento_caja(
            c, fecha=HOY, tipo="E", importe=10,
            concepto=f"{PREFIJO_CAJA} post-mutante", usuario="test")

    cur.execute("SELECT COUNT(*) FROM scintela.caja WHERE concepto = %s",
                (f"{PREFIJO_CAJA} post-mutante",))
    assert cur.fetchone()[0] == 0, "el raise tiene que haber hecho rollback"

    # Devuelvo el saldo a su lugar → la misma carga entra sin ruido.
    cur.execute("UPDATE scintela.caja SET saldo = saldo - 111 WHERE id_caja = %s",
                (ids[1],))
    conn.commit()
    with db.tx() as c:
        r = caja_helpers.insert_movimiento_caja(
            c, fecha=HOY, tipo="E", importe=10,
            concepto=f"{PREFIJO_CAJA} post-mutante", usuario="test")
    assert r["saldo_nuevo"] is not None
    assert caja_helpers.contar_quiebres(desde_fecha=HOY - timedelta(days=2)) == []


@pytest.mark.db
def test_db_borrar_del_medio_sin_reencadenar_es_exactamente_lo_que_frena(caja_limpia):
    """La trampa que se quería tapar, contra la base real."""
    import caja_helpers
    import db
    conn = caja_limpia
    ids = _sembrar_tres(conn)

    cur = conn.cursor()
    cur.execute("DELETE FROM scintela.caja WHERE id_caja = %s", (ids[1],))
    conn.commit()

    assert len(caja_helpers.contar_quiebres(desde_fecha=HOY - timedelta(days=2))) == 1

    with pytest.raises(caja_helpers.CadenaCajaRotaError), db.tx() as c:
        caja_helpers.insert_movimiento_caja(
            c, fecha=HOY, tipo="E", importe=10,
            concepto=f"{PREFIJO_CAJA} despues del borrado", usuario="test")

    # Re-encadenar es la reparación, y después la carga entra.
    with db.tx() as c:
        caja_helpers.recompute_saldos_desde(c, ancla_fecha=HOY - timedelta(days=2))
    with db.tx() as c:
        caja_helpers.insert_movimiento_caja(
            c, fecha=HOY, tipo="E", importe=10,
            concepto=f"{PREFIJO_CAJA} despues del borrado", usuario="test")
    assert caja_helpers.contar_quiebres(desde_fecha=HOY - timedelta(days=2)) == []


@pytest.mark.db
def test_db_la_regla_de_signo_de_sql_es_la_de_python(caja_limpia):
    """El SQL y el Python tienen que dar lo mismo sobre filas de verdad,
    incluidas las que este módulo no escribe: 'CB' y los importes negativos
    que dejó el dBase."""
    import caja_helpers
    conn = caja_limpia
    cur = conn.cursor()
    for tipo, imp in (("E", 100), ("S", 40), ("CB", 25), ("S", -15)):
        cur.execute(
            "INSERT INTO scintela.caja (fecha, tipo, importe, concepto, saldo) "
            "VALUES (%s, %s, %s, %s, 0)",
            (HOY, tipo, imp, f"{PREFIJO_CAJA} signo {tipo} {imp}"),
        )
    conn.commit()
    cur.execute(
        f"SELECT tipo, importe, {caja_helpers._sql_delta_firmado('c')} "
        f"  FROM scintela.caja c WHERE concepto LIKE %s",
        (f"{PREFIJO_CAJA} signo%",),
    )
    filas = cur.fetchall()
    assert len(filas) == 4
    for tipo, importe, sgn_sql in filas:
        assert float(sgn_sql) == caja_helpers._delta_firmado(tipo, importe)
