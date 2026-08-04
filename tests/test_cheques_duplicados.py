"""Detector: ¿la misma cobranza se cargó dos veces?

TMT 2026-08-04. El caso que lo motivó: Alex cargó la cobranza de ELF el 13/07
a las 21:46:53 y otra vez a las 21:47:07. Se descubrió tres semanas después,
porque la comisión de julio no cerraba contra el dBase.

Dos capas de tests, igual que el detector:

**De integración** (`@pytest.mark.db`, skip sin `PGDUP_DSN`): el SQL corrido
DE VERDAD contra un Postgres descartable. Es la única forma honesta de probar
esta query — usa `LATERAL`, `WINDOW`, `ARRAY_AGG` y `FILTER`, así que no se
puede simular con SQLite, y un fake que devuelva lo que uno espera no prueba
nada (lección del 04/08: el `_FakeDB` de `mov_doble` no filtraba por los ids
del `params` y el caso CEM pasaba en verde mientras en producción salía mal).

    PGDUP_DSN=postgresql://testpc:testpc@127.0.0.1:5432/pctest \\
        pytest tests/test_cheques_duplicados.py -q

**Puros** (corren siempre): la clasificación del veredicto y el texto que lee
la persona. No necesitan base.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import duplicados as D  # noqa: E402

DESDE, HASTA = date(2026, 6, 1), date(2026, 8, 4)

ESQUEMA = """
DROP SCHEMA IF EXISTS scintela CASCADE;
CREATE SCHEMA scintela;
CREATE TABLE scintela.cheque (
  id_cheque bigint PRIMARY KEY, no_cheque varchar(20), codigo_cli varchar(10),
  importe numeric(14,2), fechad date, no_banco integer, stat varchar(2),
  usuario_crea varchar(50));
CREATE TABLE scintela.mov_doble (
  id_mov_doble bigserial PRIMARY KEY, tipo text, origen_table text,
  origen_id bigint, batch_id uuid, fecha_creacion timestamptz,
  estado text DEFAULT 'activo');
CREATE TABLE scintela.chequextransaccion (id_cheque bigint, id_transaccion integer);
CREATE TABLE scintela.transacciones_bancarias (
  id_transaccion integer PRIMARY KEY, documento varchar(4), stat varchar(2),
  fecha date, no_banco integer);
CREATE TABLE scintela.banco_conciliacion_match (
  id bigserial PRIMARY KEY, id_transaccion integer, deshecho_en timestamp);
"""

B1 = "11111111-1111-1111-1111-111111111111"
B2 = "22222222-2222-2222-2222-222222222222"
B3 = "33333333-3333-3333-3333-333333333333"

#: 13/07 21:46:53 — el primer ELF. El segundo entró 14 segundos después.
T_ELF = datetime(2026, 7, 13, 21, 46, 53)


def _cheque(cur, id_cheque, cli, importe, fechad, *, banco=90, stat="B",
            usuario="alex", creado=None, batch=None, no_cheque=None):
    cur.execute(
        "INSERT INTO scintela.cheque VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (id_cheque, no_cheque, cli, importe, fechad, banco, stat, usuario),
    )
    if creado is not None:
        cur.execute(
            "INSERT INTO scintela.mov_doble (tipo, origen_table, origen_id, "
            "batch_id, fecha_creacion) VALUES ('cheque_creado','cheque',%s,%s,%s)",
            (id_cheque, batch, creado),
        )


def _sembrar(cur):
    # ⭐ EL CASO ELF — la misma cobranza tipeada dos veces, 14 s de diferencia.
    _cheque(cur, 1, "ELF", 735, date(2026, 7, 13), creado=T_ELF, batch=B1)
    _cheque(cur, 2, "ELF", 735, date(2026, 7, 13),
            creado=T_ELF + timedelta(seconds=14), batch=B2)

    # ⭐ LO QUE NO ES ERROR (la dueña): dos cheques de 100 en la MISMA cobranza.
    _cheque(cur, 3, "DOS", 100, date(2026, 7, 20), banco=10, no_cheque="A1",
            creado=datetime(2026, 7, 20, 10, 0, 0), batch=B3)
    _cheque(cur, 4, "DOS", 100, date(2026, 7, 20), banco=10, no_cheque="A2",
            creado=datetime(2026, 7, 20, 10, 0, 0, 300000), batch=B3)

    # Plan de cuotas traído por el import: 500 por día, dos el mismo día.
    _cheque(cur, 5, "IIA", 500, date(2026, 7, 21), banco=10, usuario="dbf-import")
    _cheque(cur, 6, "IIA", 500, date(2026, 7, 21), banco=10, usuario="dbf-import")

    # Cheques en cartera (Z) repetidos: todavía no son plata, no van al detector.
    _cheque(cur, 7, "CAR", 900, date(2026, 7, 25), banco=10, stat="Z",
            creado=datetime(2026, 7, 25, 9, 0, 0), batch=B1)
    _cheque(cur, 8, "CAR", 900, date(2026, 7, 25), banco=10, stat="Z",
            creado=datetime(2026, 7, 25, 9, 5, 0), batch=B2)

    # El mismo cobro por las dos puertas: lo trajo el import Y alguien además
    # lo tipeó en PC. Doble conteo de verdad — tiene que salir.
    _cheque(cur, 11, "MIX", 1200, date(2026, 7, 30), banco=10,
            usuario="dbf-import")
    _cheque(cur, 12, "MIX", 1200, date(2026, 7, 30), banco=10, no_cheque="7788",
            creado=datetime(2026, 8, 3, 11, 0, 0), batch=B1)

    # Altas VIEJAS, sin `batch_id` (738 de los 2.273 `cheque_creado` están así).
    # Acá el batch no puede decidir nada y manda la ventana de tiempo — es el
    # único lugar donde `VENTANA_MISMA_COBRANZA_SEG` cambia el resultado.
    _cheque(cur, 9, "OLD", 640, date(2026, 7, 28),
            creado=datetime(2026, 7, 28, 15, 10, 0))
    _cheque(cur, 10, "OLD", 640, date(2026, 7, 28),
            creado=datetime(2026, 7, 28, 15, 10, 14))


@pytest.fixture
def pg():
    dsn = os.environ.get("PGDUP_DSN")
    if not dsn:
        pytest.skip("Sin PGDUP_DSN: los tests de integración del detector no corren.")
    if "test" not in dsn.rsplit("/", 1)[-1].lower():
        pytest.fail("PGDUP_DSN tiene que apuntar a una base de TEST: el fixture "
                    "hace DROP SCHEMA scintela CASCADE.")
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(ESQUEMA)
        _sembrar(cur)
    conn.commit()

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    class _DB:
        def fetch_all(self, sql, params=None, conn=None):
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    original = D.db
    D.db = _DB()
    try:
        yield conn
    finally:
        D.db = original
        conn.rollback()
        conn.close()


def _grupos(**kw):
    return {g["cli"]: g for g in D.sospechosos(DESDE, HASTA, **kw)}


# ---------------------------------------------------------------------------
# Capa 1 — a quién señala y a quién no
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_agarra_la_cobranza_cargada_dos_veces(pg):
    g = _grupos()
    assert "ELF" in g, "el caso que motivó el detector se le escapó"
    assert g["ELF"]["n_cheques"] == 2
    assert g["ELF"]["n_cobranzas"] == 2
    assert sorted(g["ELF"]["ids"]) == [1, 2]


@pytest.mark.db
def test_dos_cheques_iguales_de_la_MISMA_cobranza_no_son_error(pg):
    """⭐ La regla que puso la dueña.

    *"si pongo 100 y otros cheque de 100 en la misma cobranza claramente no va
    a ser error"*. Son dos papeles de verdad entregados juntos y tipeados a
    propósito — la falla es la cobranza REPETIDA, no el importe repetido.
    """
    assert "DOS" not in _grupos()


@pytest.mark.db
def test_lo_que_trajo_el_import_del_dbase_no_se_denuncia(pg):
    """IIA paga 500 por día. Son 11 días de julio así, medidos en producción.

    Un duplicado del import es un duplicado DEL DBASE, y ahí manda el dBase.
    Una alarma que grita 50 veces por día deja de leerse en una semana.
    """
    assert "IIA" not in _grupos()


@pytest.mark.db
def test_import_MAS_carga_a_mano_del_mismo_cobro_si_se_denuncia(pg):
    """⭐ El otro doble conteo: el mismo cobro entró por las dos puertas.

    El import lo trajo del dBase y alguien además lo tipeó en PC. Alcanza con
    que UNA de las copias sea de una persona — exigir dos a mano dejaba pasar
    este caso, que es de los más caros porque nadie mira las dos pantallas
    juntas.
    """
    g = _grupos()
    assert "MIX" in g
    assert sorted(g["MIX"]["ids"]) == [11, 12]
    assert g["MIX"]["n_a_mano"] == 1


@pytest.mark.db
def test_los_cheques_todavia_en_cartera_no_entran(pg):
    """`Z` no es plata cobrada: no bajó deuda ni pagó comisión.

    El ruido medido vive casi entero acá — planes de cuotas posdatados con la
    misma `fechad` (MMM: 12 cheques de 3.100 con la misma fecha).
    """
    assert "CAR" not in _grupos()


@pytest.mark.db
def test_sin_batch_la_ventana_de_tiempo_separa_las_dos_cargas(pg):
    """738 de los 2.273 `cheque_creado` no tienen `batch_id` (altas viejas).

    Ahí el batch no puede decidir nada y lo único que queda es el tiempo: dos
    altas de la MISMA cobranza salen de una sola transacción y cierran juntas
    (medido: los 316 batches multi-cheque, todos en <=2 s).
    """
    assert "OLD" in _grupos()


@pytest.mark.db
def test_con_una_ventana_grande_ese_caso_se_pierde(pg):
    """⭐ Por qué la ventana es de 2 segundos y no de un minuto.

    ELF fueron 14 s entre una carga y la otra — el mismo espaciado que `OLD`.
    Con una ventana "prudente" de 60 s las dos cobranzas se leen como una sola
    y el detector calla justo en el caso para el que se escribió.
    """
    assert "OLD" not in _grupos(ventana_seg=60)


@pytest.mark.db
def test_el_batch_manda_sobre_la_ventana_de_tiempo(pg):
    """ELF tiene dos `batch_id` distintos: son dos cobranzas, dure lo que dure.

    Al revés también: los dos cheques de `DOS` comparten batch y por más que
    se agrande la ventana siguen siendo una sola cobranza. El tiempo es sólo
    el fallback de lo que no tiene batch.
    """
    g = _grupos(ventana_seg=60)
    assert "ELF" in g, "el batch distinto tiene que ganarle a la ventana"
    assert "DOS" not in g


# ---------------------------------------------------------------------------
# Capa 2 — el veredicto contra el extracto
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_sin_deposito_el_veredicto_es_no_se(pg):
    """No es lo mismo "el banco no lo tiene" que "no tengo cómo saberlo".

    Un cheque sin `chequextransaccion` no tiene depósito que conciliar
    (efectivo, anticipo, o los del import que nunca trajeron el link).
    Contarlo como faltante sería mentir con cara de certeza.
    """
    assert D.veredicto_extracto([1, 2]) == {1: D.SIN_DEPOSITO, 2: D.SIN_DEPOSITO}


def _dia_cerrado(cur, *, fecha=date(2026, 7, 13), banco=10, n=9, desde_id=900):
    """Un día "cerrado": el resto de sus depósitos ya está conciliado.

    Sin esto, el día entero cuenta como sin conciliar y el veredicto es
    `PERIODO_ABIERTO` — que es lo correcto, y por eso los tests que quieren
    probar "no está en el banco" tienen que cerrar el día primero.
    """
    for i in range(n):
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (%s,'DE','*',%s,%s)", (desde_id + i, fecha, banco))


@pytest.mark.db
def test_conciliado_por_match_vivo_cuenta_como_acreditado(pg):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
        cur.execute("INSERT INTO scintela.banco_conciliacion_match "
                    "(id_transaccion) VALUES (500)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.EN_EL_BANCO


@pytest.mark.db
def test_un_match_deshecho_ya_no_vale(pg):
    with pg.cursor() as cur:
        _dia_cerrado(cur)
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
        cur.execute("INSERT INTO scintela.banco_conciliacion_match "
                    "(id_transaccion, deshecho_en) VALUES (500, now())")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.NO_EN_EL_BANCO


@pytest.mark.db
def test_el_stat_asterisco_del_dbase_tambien_vale(pg):
    """El dBase marca conciliado con `stat='*'` y no crea fila de match.

    Sin esta rama, todo lo que concilió el FoxPro saldría como "no está en el
    banco".
    """
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (501,'DE','*',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (2,501)")
    pg.commit()
    assert D.veredicto_extracto([2])[2] == D.EN_EL_BANCO


@pytest.mark.db
def test_solo_mira_movimientos_DE(pg):
    """Un `CH` o una `ND` no son el depósito de este cheque."""
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (502,'ND','*',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,502)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.SIN_DEPOSITO


@pytest.mark.db
def test_un_dia_que_todavia_no_se_concilio_no_acusa_a_nadie(pg):
    """⭐ El falso positivo que casi se publica.

    Medido el 04/08 probando este detector contra MTM: sus dos depósitos del
    03/08 estaban sin conciliar y el veredicto ingenuo los daba a los dos por
    inexistentes. No faltaban — el extracto de agosto no se había subido. Ese
    día el banco 10 tenía 44 depósitos y 2 conciliados; el 31/07, 52 de 54.

    "Sin conciliar" y "no está en el banco" no son lo mismo, y confundirlos
    convierte cada cierre de mes en una lluvia de alarmas falsas.
    """
    with pg.cursor() as cur:
        for i in range(9):  # el día entero sin conciliar
            cur.execute("INSERT INTO scintela.transacciones_bancarias "
                        "VALUES (%s,'DE','A',%s,10)", (800 + i, date(2026, 8, 3)))
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','A',%s,10)", (date(2026, 8, 3),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.PERIODO_ABIERTO


@pytest.mark.db
def test_con_el_dia_cerrado_el_mismo_cheque_si_se_acusa(pg):
    """Mismo cheque, misma falta de conciliación: lo único que cambia es que
    el resto del día SÍ está conciliado. Ahí la ausencia significa algo."""
    with pg.cursor() as cur:
        _dia_cerrado(cur, fecha=date(2026, 8, 3))
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','A',%s,10)", (date(2026, 8, 3),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.NO_EN_EL_BANCO


@pytest.mark.db
def test_la_cobertura_se_mide_del_mismo_banco_y_el_mismo_dia(pg):
    """Otro banco conciliado no dice nada de éste."""
    with pg.cursor() as cur:
        _dia_cerrado(cur, fecha=date(2026, 8, 3), banco=91)
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','A',%s,10)", (date(2026, 8, 3),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.PERIODO_ABIERTO


@pytest.mark.db
def test_la_cobertura_se_mide_del_mismo_DIA(pg):
    """Que ayer esté conciliado no dice nada de hoy.

    Sin el corte por fecha, un banco con meses conciliados hace pasar por
    "cerrado" al día de hoy, que es justo el que todavía no se subió.
    """
    with pg.cursor() as cur:
        _dia_cerrado(cur, fecha=date(2026, 7, 31))
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','A',%s,10)", (date(2026, 8, 3),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
    pg.commit()
    assert D.veredicto_extracto([1])[1] == D.PERIODO_ABIERTO


@pytest.mark.db
def test_revisar_junta_las_dos_capas(pg):
    with pg.cursor() as cur:
        _dia_cerrado(cur)
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (500,'DE','*',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.transacciones_bancarias "
                    "VALUES (501,'DE','',%s,10)", (date(2026, 7, 13),))
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (1,500)")
        cur.execute("INSERT INTO scintela.chequextransaccion VALUES (2,501)")
    pg.commit()
    por_cli = {f["cli"]: f for f in D.revisar(DESDE, HASTA)}
    elf = por_cli["ELF"]
    assert elf["ids_sin_respaldo"] == [2], "el que el banco no acreditó es el 2"
    assert elf["monto_en_riesgo"] == 735.0
    assert "no aparecen en el extracto" in elf["veredicto"]
    # El grupo sin ningún depósito conciliable no se disfraza de hallazgo.
    assert por_cli["OLD"]["monto_en_riesgo"] == 0
    assert "a mano" in por_cli["OLD"]["veredicto"]


# ---------------------------------------------------------------------------
# Puros — corren siempre, sin base
# ---------------------------------------------------------------------------

def test_clasificar_sin_movimientos_es_no_se():
    assert D.clasificar_veredicto(0, 0) == D.SIN_DEPOSITO


def test_clasificar_todos_conciliados_es_en_el_banco():
    assert D.clasificar_veredicto(2, 2) == D.EN_EL_BANCO


def test_clasificar_alguno_sin_conciliar_con_el_dia_cerrado_acusa():
    assert D.clasificar_veredicto(2, 1, 1) == D.NO_EN_EL_BANCO


def test_clasificar_sin_conciliar_pero_con_el_dia_abierto_no_acusa():
    """⭐ Las dos formas de "no sé" no se mezclan: "el día no está conciliado"
    se arregla subiendo el extracto; "este cheque no tiene depósito" se
    arregla mirándolo a mano."""
    assert D.clasificar_veredicto(2, 1, 0) == D.PERIODO_ABIERTO


def test_el_umbral_del_dia_deja_lugar_a_los_dos_lados():
    """Medido: los días cerrados van 95-100 % y los abiertos 0-5 %. Un umbral
    pegado a un borde (0 o 100) elimina una de las dos respuestas."""
    assert 5 < D.UMBRAL_DIA_CONCILIADO_PCT < 95


def test_el_monto_en_riesgo_es_lo_que_sobra_y_no_el_total():
    """Tres cheques de 500 de los que sobra uno son 500 de problema, no 1.500.

    Inflar el número hace que la alarma se lea como catástrofe y se apague.
    """
    g = {"imp": 500.0, "ids": [1, 2, 3]}
    r = D.resumir(g, {1: D.EN_EL_BANCO, 2: D.EN_EL_BANCO, 3: D.NO_EN_EL_BANCO})
    assert r["monto_en_riesgo"] == 500.0
    assert r["ids_sin_respaldo"] == [3]


def test_cuando_el_banco_acredito_todos_lo_dice():
    g = {"imp": 500.0, "ids": [1, 2]}
    r = D.resumir(g, {1: D.EN_EL_BANCO, 2: D.EN_EL_BANCO})
    assert r["monto_en_riesgo"] == 0
    assert "cobros distintos" in r["veredicto"]


def test_cuando_no_hay_dato_no_finge_certeza():
    g = {"imp": 500.0, "ids": [1, 2]}
    r = D.resumir(g, {})
    assert r["monto_en_riesgo"] == 0
    assert "a mano" in r["veredicto"]


def test_el_periodo_abierto_se_dice_con_todas_las_letras():
    """Que se lea "todavía no se concilió" y no "falta plata"."""
    g = {"imp": 300.0, "ids": [1, 2]}
    r = D.resumir(g, {1: D.PERIODO_ABIERTO, 2: D.PERIODO_ABIERTO})
    assert r["monto_en_riesgo"] == 0
    assert r["ids_periodo_abierto"] == [1, 2]
    assert "no se concilió" in r["veredicto"]


def test_la_ventana_de_la_misma_cobranza_es_chica():
    """Documenta el número medido: los 316 batches multi-cheque cierran en
    <=2 s, y ELF —dos cobranzas— fueron 14 s. Subirlo tapa el caso real."""
    assert D.VENTANA_MISMA_COBRANZA_SEG <= 2


def test_el_import_no_cuenta_como_persona():
    assert "dbf-import" in D.USUARIOS_IMPORT
    assert "reconcile-dbf" in D.USUARIOS_IMPORT
