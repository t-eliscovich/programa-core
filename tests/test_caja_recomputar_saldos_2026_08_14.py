"""`caja.queries.recomputar_saldos()` DESPUÉS de la fusión. TMT 2026-08-14.

⭐ POR QUÉ. Hasta hoy la caja tenía DOS re-encadenados: el walk-forward de
`caja_helpers.recompute_saldos_desde` (con candado) y un segundo walk completo
adentro de `caja.queries.recomputar_saldos()`, con su propio `_signed` y sin
candado. Dos definiciones del mismo saldo es exactamente la trampa que la caja
ya pagó con el SIGNO —cuatro definiciones, una peleada con las otras tres— y
que el candado nuevo habría "corregido" restando plata que sí entró.

Quedó una sola: `recomputar_saldos()` delega el walk en el helper y aporta lo
único que el helper no tiene, que es **de dónde arranca**. Estos tests clavan
esa diferencia, que es la que se podía perder al fusionar:

  1. EL OPENING. En caja la PRIMERA FILA ES LA DECLARACIÓN DE LA APERTURA
     (`saldo_actual()` y `salcaj()` calculan el opening como
     `saldo(1ra) − delta(1ra)`; no hay ningún segundo número que la desmienta).
     Por eso el ancla es la SEGUNDA fila: anclar en la fecha más vieja hace que
     el helper no encuentre fila anterior, arranque de 0 y le coma el opening a
     la caja entera. Es lo que hizo la mig 0091 y lo que la 0092 tuvo que
     reponer a mano (80.053,99).
  2. EL IMPORTE CRUDO, SIN `abs()`. Las filas legacy del 17→30/04 llegaron del
     dBase con `importe` NEGATIVO (8 en el histórico: 6 'E' y 2 'S') y las dos
     lecturas de las que sale la plata publicada las suman tal cual. Con
     `abs()` esas filas se estampan para el otro lado — la 0091 lo hizo y dejó
     un offset de −40.752,52.

Los dos casos están acá arriba con el fake (rápidos, sin base) y abajo contra
Postgres de verdad, incluido el mutante.
"""
from __future__ import annotations

import contextlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_bank_helpers import _FakeCajaDB  # noqa: E402


class _FakeCajaConSaldos(_FakeCajaDB):
    """El fake compartido + la lectura `SELECT id_caja, saldo` del reflote.

    Se extiende ACÁ y no en `tests/test_bank_helpers.py` para no tocarle el
    fake a los tests de banco y del candado, que ya lo comparten.
    """

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if s.startswith("select id_caja, saldo from scintela.caja"):
            return [
                {"id_caja": f["id_caja"], "saldo": f["saldo"]}
                for f in sorted(self.filas,
                                key=lambda f: (f["fecha"], f["id_caja"]))
            ]
        return super().fetch_all(sql, params, conn)


def _fila(id_caja, fecha, tipo, importe, saldo, concepto="x"):
    return {"id_caja": id_caja, "fecha": fecha, "tipo": tipo,
            "importe": importe, "saldo": saldo, "concepto": concepto}


def _instalar(monkeypatch, filas):
    import db as db_mod
    fake = _FakeCajaConSaldos(list(filas))
    fake.apply_to(monkeypatch, db_mod)
    monkeypatch.setattr(
        db_mod, "tx", lambda: contextlib.nullcontext(object()))
    return fake


def _saldos(fake) -> list[float]:
    return [float(f["saldo"])
            for f in sorted(fake.filas, key=lambda f: (f["fecha"], f["id_caja"]))]


# ── 1) el opening ────────────────────────────────────────────────────────

def test_el_opening_de_la_primera_fila_sobrevive_al_reflote(monkeypatch):
    """⭐ LA DIFERENCIA QUE IMPORTA.

    La caja arranca con 5.000 que ya estaban en el cajón antes del primer
    movimiento cargado: la fila 1 entra 100 y queda en 5.100, o sea DECLARA
    que había 5.000. Alguien corre el saldo de la fila del medio; el reflote
    tiene que reconstruir la cadena SIN tocar esa declaración.

    Si el walk se anclara en la fecha más vieja (que es lo que sale natural al
    fusionar: `recompute_saldos_desde(ancla_fecha=MIN(fecha))`), no habría fila
    anterior de dónde arrancar, empezaría de 0 y la caja entera bajaría 5.000.
    """
    import modules.caja.queries as q

    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 8, 10), "E", 100, 5100.0, "apertura declarada"),
        _fila(2, date(2026, 8, 11), "E", 300, 9999.0, "cobro con el saldo corrido"),
        _fila(3, date(2026, 8, 12), "S", 200, 5200.0, "pago flete"),
    ])

    res = q.recomputar_saldos()

    assert _saldos(fake) == [5100.0, 5400.0, 5200.0]
    assert _saldos(fake)[0] == 5100.0, (
        "la primera fila ES la apertura: el reflote no la puede reescribir")
    assert res["saldo_final"] == 5200.0
    assert res["filas"] == 3
    assert res["cambios"] == 1


def test_cadena_sana_no_mueve_ningun_saldo(monkeypatch):
    """El otro lado: un reflote que reescribe siempre no se distingue de uno
    que reescribe mal. Con la cadena sana, `cambios` tiene que ser 0."""
    import modules.caja.queries as q

    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 8, 10), "E", 100, 5100.0),
        _fila(2, date(2026, 8, 11), "E", 300, 5400.0),
        _fila(3, date(2026, 8, 12), "S", 200, 5200.0),
    ])

    res = q.recomputar_saldos()

    assert _saldos(fake) == [5100.0, 5400.0, 5200.0]
    assert res["cambios"] == 0


def test_caja_vacia_devuelve_el_dict_en_cero(monkeypatch):
    """Sin filas no hay ancla, y el helper sin ancla revienta a propósito
    (rebobinar desde 0 borra el opening). La firma pública sigue devolviendo
    dict, que es lo que `views.lista` espera."""
    import modules.caja.queries as q

    _instalar(monkeypatch, [])
    assert q.recomputar_saldos() == {"filas": 0, "saldo_final": 0.0,
                                     "cambios": 0}


def test_una_sola_fila_no_se_toca(monkeypatch):
    """Con una fila no hay nada que re-encadenar: esa fila ES la apertura."""
    import modules.caja.queries as q

    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 8, 10), "E", 100, 5100.0),
    ])
    res = q.recomputar_saldos()
    assert _saldos(fake) == [5100.0]
    assert res == {"filas": 1, "saldo_final": 5100.0, "cambios": 0}


# ── 2) las filas legacy con importe NEGATIVO ─────────────────────────────

def test_filas_legacy_con_importe_negativo_no_pasan_por_abs(monkeypatch):
    """⭐ 8 filas del 17→30/04 traen `importe` negativo (6 'E' y 2 'S').

    El importe va CRUDO: 'E' suma lo que dice (aunque sea negativo) y 'S'
    resta lo que dice (o sea una 'S' de −30 SUMA 30). Es lo que hacen
    `saldo_actual()` y `salcaj()`, que son de donde sale la plata publicada.
    Con `abs()` esta cadena daría [5100, 5150, 5120] — que es exactamente el
    desvío que dejó la mig 0091.
    """
    import modules.caja.queries as q

    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 4, 17), "E", 100, 5100.0, "apertura declarada"),
        _fila(2, date(2026, 4, 20), "E", -50, 0.0, "legacy dBase E negativa"),
        _fila(3, date(2026, 4, 30), "S", -30, 0.0, "legacy dBase S negativa"),
    ])

    q.recomputar_saldos()

    assert _saldos(fake) == [5100.0, 5050.0, 5080.0]


def test_con_abs_esa_misma_cadena_daria_otra_cosa(monkeypatch):
    """El contrafáctico del test de arriba, para que sus números no sean
    decorado: si el delta se calculara con `abs()`, las mismas tres filas
    darían [5100, 5150, 5120] en vez de [5100, 5050, 5080].

    La regla de signo ya no vive en este módulo —se borró el `_signed` local y
    quedó `caja_helpers._delta_firmado`, que es la única—, así que el mutante
    se hace acá, parchando el helper, en vez de editarlo. Es la diferencia
    exacta que la mig 0091 metió con ABS y la 0092 tuvo que revertir.
    """
    import caja_helpers
    import modules.caja.queries as q

    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 4, 17), "E", 100, 5100.0),
        _fila(2, date(2026, 4, 20), "E", -50, 0.0),
        _fila(3, date(2026, 4, 30), "S", -30, 0.0),
    ])
    monkeypatch.setattr(
        caja_helpers, "_delta_firmado",
        lambda tipo, importe: (caja_helpers.signo_tipo(tipo)
                               * abs(float(importe or 0))))

    q.recomputar_saldos()

    assert _saldos(fake) == [5100.0, 5150.0, 5120.0]


# ── 4) contra Postgres de verdad ─────────────────────────────────────────
#
# Los de arriba corren sobre el fake. Abajo va lo mismo contra la base, más
# el MUTANTE: rompo un saldo a mano y el reflote lo tiene que dejar en su
# lugar (y no romper el resto de la caja al pasar).

PREFIJO = "RECOMPUTAR-CAJA-TEST"
# Bien atrás en el tiempo: estas filas tienen que quedar ANTES de todo lo que
# haya en la caja, porque lo que se está probando es de dónde arranca el walk.
D0 = date(2019, 1, 2)


@pytest.fixture
def caja_restaurable(real_db_conn, migrated_db):
    """Siembra al PRINCIPIO de la caja y deja todo como estaba.

    La caja es UNA sola cadena compartida por toda la suite: meter filas más
    viejas que todas re-encadena también las de los demás. Por eso el teardown
    no borra nada más que lo suyo y devuelve cada `saldo` al valor exacto que
    tenía. [[feedback_commitear_antes_de_mutar_siempre]]
    """
    cur = real_db_conn.cursor()
    cur.execute("SELECT id_caja, saldo FROM scintela.caja")
    snapshot = cur.fetchall()
    yield real_db_conn
    cur.execute("DELETE FROM scintela.caja WHERE concepto LIKE %s",
                (f"{PREFIJO}%",))
    for id_caja, saldo in snapshot:
        cur.execute("UPDATE scintela.caja SET saldo = %s WHERE id_caja = %s",
                    (saldo, id_caja))
    real_db_conn.commit()


def _sembrar(conn, filas) -> list[int]:
    """INSERT crudo: acá se prueba el RE-encadenado, no el alta."""
    cur = conn.cursor()
    ids = []
    for i, (dias, tipo, importe, saldo) in enumerate(filas):
        cur.execute(
            "INSERT INTO scintela.caja (fecha, tipo, importe, concepto, saldo) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id_caja",
            (D0 + timedelta(days=dias), tipo, importe, f"{PREFIJO} {i}", saldo),
        )
        ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def _leer(conn, ids) -> list[float]:
    cur = conn.cursor()
    cur.execute("SELECT id_caja, saldo FROM scintela.caja WHERE id_caja = ANY(%s)",
                (list(ids),))
    d = {i: float(s) for i, s in cur.fetchall()}
    return [d[i] for i in ids]


@pytest.mark.db
def test_db_el_reflote_reconstruye_la_cadena_sin_tocar_el_opening(caja_restaurable):
    """⭐ EL MUTANTE, contra la base. Rompo el saldo del medio a mano; el
    reflote lo devuelve a su lugar y deja la apertura donde estaba."""
    import modules.caja.queries as q
    conn = caja_restaurable
    ids = _sembrar(conn, [
        (0, "E", 100, 5100),    # declara opening = 5.000
        (1, "E", 300, 5400),
        (2, "S", 200, 5200),
    ])

    cur = conn.cursor()
    cur.execute("UPDATE scintela.caja SET saldo = saldo + 777 WHERE id_caja = %s",
                (ids[1],))
    conn.commit()
    assert _leer(conn, ids) == [5100.0, 6177.0, 5200.0]

    res = q.recomputar_saldos()

    assert _leer(conn, ids) == [5100.0, 5400.0, 5200.0]
    assert res["cambios"] >= 1
    # y la cadena entera (no sólo mis tres filas) quedó cerrada
    import caja_helpers
    assert caja_helpers.contar_quiebres(desde_fecha=D0) == []


@pytest.mark.db
def test_db_las_filas_legacy_negativas_se_suman_crudas(caja_restaurable):
    """Las 8 filas del histórico con `importe < 0`, contra la base: 'E' −50
    BAJA el saldo y 'S' −30 lo SUBE. Con `abs()` daría [5100, 5150, 5120]."""
    import modules.caja.queries as q
    conn = caja_restaurable
    ids = _sembrar(conn, [
        (0, "E", 100, 5100),
        (1, "E", -50, 0),
        (2, "S", -30, 0),
    ])

    q.recomputar_saldos()

    assert _leer(conn, ids) == [5100.0, 5050.0, 5080.0]


@pytest.mark.db
def test_db_reflotar_dos_veces_no_mueve_nada_la_segunda(caja_restaurable):
    """Idempotencia: la segunda corrida no cambia un solo saldo. Un reflote
    que mueve plata cada vez que se lo llama es un reflote roto."""
    import modules.caja.queries as q
    conn = caja_restaurable
    ids = _sembrar(conn, [
        (0, "E", 100, 5100),
        (1, "E", 300, 9999),
        (2, "S", 200, 5200),
    ])

    primera = q.recomputar_saldos()
    saldos_1 = _leer(conn, ids)
    segunda = q.recomputar_saldos()

    assert primera["cambios"] >= 1
    assert segunda["cambios"] == 0
    assert _leer(conn, ids) == saldos_1 == [5100.0, 5400.0, 5200.0]
    assert segunda["saldo_final"] == primera["saldo_final"]
