"""Migración 0155 — borrar las fichas duplicadas y PROHIBIR códigos repetidos.

Dos capas, a propósito:

**Estáticos** (corren siempre, sin Postgres): que las listas de ids sean las
que se revisaron a mano, que no se pisen entre ellas, y que el índice
normalice igual que los JOINs del sistema. Son baratos y son los que se
rompen si alguien edita la migración de apuro.

**De integración** (`@pytest.mark.db`, skip sin `PG0155_DSN`): la migración
corrida DE VERDAD contra un Postgres descartable, con un esquema sintético
que reproduce la forma de la data real. Son los que prueban el guard, y en
particular los **tres modos de falla**: si una ficha juntó un movimiento,
perdió su hermana, o tiene una proforma, no se borra NADA (una sola
transacción). Un guard que no se probó fallando no es un guard.

Para correrlos::

    PG0155_DSN=postgresql://user@127.0.0.1:5433/test_0155 pytest tests/test_migracion_0155_codigos_unicos.py -q

El DSN tiene que apuntar a una base **de test** (el fixture exige "test" en el
nombre) porque hace `DROP SCHEMA scintela CASCADE`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SQL_PATH = RAIZ / "migrations" / "0155_cliente_codigo_sin_duplicados.sql"
SQL = SQL_PATH.read_text(encoding="utf-8")

#: Las 15 fichas sobrantes que la revisión del 04/08 dio por seguras de borrar.
#: Cada una es el MISMO cliente (mismo RUC) que ya está cargado con otro código.
CONDENADAS = {
    29243, 29347, 31346, 31994, 32143, 30082, 31453, 29390,
    29483, 29191, 29173, 29336, 29581, 32295, 31707,
}

#: Los 7 pares que siguen abiertos: uno de cada par queda fuera del índice.
EXCEPTUADAS = {29142, 30547, 31469, 29048, 31038, 32066, 31562}


def _ids_del_array() -> set[int]:
    """Los ids del `ARRAY[...]` del guard, leídos del SQL."""
    m = re.search(r"v_condenadas\s+INTEGER\[\]\s*:=\s*ARRAY\s*\[(.*?)\]", SQL, re.DOTALL)
    assert m, "No encontré el ARRAY de fichas condenadas en la migración."
    return {int(n) for n in re.findall(r"\b(\d{4,})\b", re.sub(r"--[^\n]*", "", m.group(1)))}


def _ids_exceptuados() -> set[int]:
    """Los ids del `NOT IN (...)` del índice."""
    m = re.search(r"id_cliente\s+NOT\s+IN\s*\((.*?)\)", SQL, re.DOTALL)
    assert m, "No encontré el NOT IN del índice."
    return {int(n) for n in re.findall(r"\d+", m.group(1))}


# ---------------------------------------------------------------------------
# Estáticos
# ---------------------------------------------------------------------------


def test_borra_exactamente_las_15_fichas_revisadas():
    assert _ids_del_array() == CONDENADAS
    assert len(CONDENADAS) == 15


def test_el_indice_exceptua_exactamente_los_7_pares_abiertos():
    assert _ids_exceptuados() == EXCEPTUADAS
    assert len(EXCEPTUADAS) == 7


def test_ninguna_ficha_esta_condenada_y_exceptuada_a_la_vez():
    """Un id en las dos listas sería un agujero del índice sin fila detrás.

    Peor que inútil: la excepción quedaría para siempre en el índice
    "protegiendo" a una ficha que la propia migración borró, y nadie sabría
    por qué está.
    """
    assert not (CONDENADAS & _ids_exceptuados())


def test_el_indice_normaliza_igual_que_los_JOINs_del_sistema():
    """`'lec '` y `'LEC'` son el mismo código para la factura; también acá.

    Si el índice fuera sobre `codigo_cli` pelado, alcanzaría con un espacio
    al final para volver a crear el duplicado que estamos prohibiendo.
    """
    m = re.search(r"CREATE UNIQUE INDEX.*?;", SQL, re.DOTALL)
    assert m, "No encontré el CREATE UNIQUE INDEX."
    indice = m.group(0)
    assert "UPPER(TRIM(codigo_cli))" in indice
    assert "IF NOT EXISTS" in indice, "Tiene que poder re-correrse."


def test_el_indice_deja_afuera_los_codigos_vacios():
    """Hay clientes legacy sin código: no queremos un único cliente-sin-código."""
    m = re.search(r"CREATE UNIQUE INDEX.*?;", SQL, re.DOTALL)
    assert "COALESCE(TRIM(codigo_cli), '') <> ''" in m.group(0)


@pytest.mark.parametrize("tabla", [
    "scintela.factura",
    "scintela.cheque",
    "scintela.chequesxfact",
    "scintela.retencion",
    "scintela.cobro",
    "scintela.proforma_cabecera",
])
def test_el_guard_mira_todas_las_tablas_que_cuelgan_del_cliente(tabla):
    """Olvidarse una tabla es borrar plata sin dueño y no enterarse."""
    guard = SQL.split("$$")[1]
    assert tabla in guard, f"El guard no mira {tabla}"


def test_el_guard_corta_con_exception_y_no_deja_que_explote_el_indice():
    """El error de Postgres por índice duplicado no dice cuál ficha ni por qué."""
    guard = SQL.split("$$")[1]
    assert "RAISE EXCEPTION" in guard
    assert "RAISE NOTICE" in guard, "Hace falta ver cuántas borró en la corrida."


def test_la_hermana_que_salva_a_una_ficha_no_puede_ser_otra_condenada():
    """Si no, dos condenadas se avalan entre ellas y el cliente desaparece."""
    guard = SQL.split("$$")[1]
    assert "NOT (h.id_cliente = ANY (v_condenadas))" in guard


# ---------------------------------------------------------------------------
# Integración — Postgres de verdad
# ---------------------------------------------------------------------------

ESQUEMA = """
DROP SCHEMA IF EXISTS scintela CASCADE;
CREATE SCHEMA scintela;

CREATE TABLE scintela.cliente (
    id_cliente  INTEGER PRIMARY KEY,
    codigo_cli  VARCHAR(10),
    nombre      VARCHAR(200),
    ruc         VARCHAR(20),
    direccion1  VARCHAR(200)
);
CREATE TABLE scintela.factura      (id SERIAL PRIMARY KEY, codigo_cli VARCHAR(10));
CREATE TABLE scintela.cheque       (id SERIAL PRIMARY KEY, codigo_cli VARCHAR(10));
CREATE TABLE scintela.chequesxfact (id SERIAL PRIMARY KEY, codigo_cli VARCHAR(10));
CREATE TABLE scintela.retencion    (id SERIAL PRIMARY KEY, codigo_cli VARCHAR(10));
CREATE TABLE scintela.cobro        (id SERIAL PRIMARY KEY, codigo_cli VARCHAR(10));
CREATE TABLE scintela.proforma_cabecera (
    id SERIAL PRIMARY KEY,
    id_cliente INTEGER REFERENCES scintela.cliente(id_cliente)
);
"""

#: (id, código) de cada condenada — el código repetido tal cual está en PC.
CONDENADAS_CON_CODIGO = [
    (29243, "ALP"), (29347, "BEH"), (31346, "BSA"), (31994, "DIC"),
    (32143, "FSB"), (30082, "JAQ"), (31453, "JIL"), (29390, "KAB"),
    (29483, "KAB"), (29191, "LUL"), (29173, "NOB"), (29336, "NOB"),
    (29581, "NUS"), (32295, "APD"), (31707, "YEL"),
]

#: Los 7 pares abiertos: (id exceptuado, id hermano indexado, código).
PARES_ABIERTOS = [
    (29142, 28929, "BLP"), (30547, 29114, "BRC"), (31469, 29893, "JQS"),
    (29048, 29008, "LEC"), (31038, 29995, "YMO"), (32066, 31088, "JRP"),
    (31562, 29170, "MTE"),
]


def _sembrar(cur) -> None:
    """Data sintética con la MISMA forma que la real.

    Cada condenada tiene una hermana con su mismo RUC y otro código (que es
    justo lo que la hace segura de borrar), y los 7 pares abiertos son dos
    RUCs distintos compartiendo un código.
    """
    for i, (id_c, cod) in enumerate(CONDENADAS_CON_CODIGO):
        ruc = f"{1100000000 + i}001"
        cur.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
            "VALUES (%s, %s, %s, %s)", (id_c, cod, f"CLIENTE {cod} {i}", ruc))
        # La hermana: mismo RUC, código bueno, NO está en la lista.
        cur.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
            "VALUES (%s, %s, %s, %s)", (90000 + i, f"H{i:02d}", f"CLIENTE {cod} {i}", ruc))
    for j, (id_ex, id_herm, cod) in enumerate(PARES_ABIERTOS):
        cur.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
            "VALUES (%s, %s, %s, %s)", (id_ex, cod, f"ABIERTO {cod} A", f"{2200000000 + j}001"))
        cur.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
            "VALUES (%s, %s, %s, %s)", (id_herm, cod, f"ABIERTO {cod} B", f"{2300000000 + j}001"))
    # Dos legacy sin código: el índice los tiene que ignorar a los dos.
    cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
                "VALUES (70001, '', 'LEGACY UNO', '1900000001001')")
    cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
                "VALUES (70002, NULL, 'LEGACY DOS', '1900000002001')")


@pytest.fixture
def pg():
    """Conexión a un Postgres descartable con el esquema sintético sembrado.

    Skip —no error— si no hay DSN: la suite tiene que poder correr en el CI
    sin Postgres.
    """
    dsn = os.environ.get("PG0155_DSN")
    if not dsn:
        pytest.skip("Sin PG0155_DSN: los tests de integración de la 0155 no corren.")
    if "test" not in dsn.rsplit("/", 1)[-1].lower():
        pytest.fail("PG0155_DSN tiene que apuntar a una base de TEST: el fixture "
                    "hace DROP SCHEMA scintela CASCADE.")
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(ESQUEMA)
        _sembrar(cur)
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _correr(conn):
    """Aplica la migración como lo hace `scripts/migrate.py::_apply_sql`.

    Todo el archivo en UN `execute` → una sola transacción implícita. Es lo
    que hace que un `RAISE EXCEPTION` en el guard no deje nada a medias.
    """
    with conn.cursor() as cur:
        cur.execute(SQL)
    conn.commit()


def _n_clientes(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM scintela.cliente")
        return cur.fetchone()[0]


def _existe(conn, id_cliente: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM scintela.cliente WHERE id_cliente = %s", (id_cliente,))
        return cur.fetchone() is not None


@pytest.mark.db
def test_corrida_normal_borra_las_15_y_deja_las_hermanas(pg):
    antes = _n_clientes(pg)
    _correr(pg)
    assert _n_clientes(pg) == antes - 15
    for id_c, _cod in CONDENADAS_CON_CODIGO:
        assert not _existe(pg, id_c), f"la ficha {id_c} tenía que borrarse"
    for i in range(len(CONDENADAS_CON_CODIGO)):
        assert _existe(pg, 90000 + i), "la hermana con el código bueno no se toca"


@pytest.mark.db
def test_dice_cuantas_borro(pg):
    """El NOTICE es lo único que la dueña ve en /admin/migraciones."""
    _correr(pg)
    assert any("borradas en esta corrida: 15" in n for n in pg.notices), pg.notices


@pytest.mark.db
def test_es_idempotente(pg):
    _correr(pg)
    n = _n_clientes(pg)
    _correr(pg)  # segunda corrida: no borra nada y el índice ya existe
    assert _n_clientes(pg) == n


@pytest.mark.db
def test_el_indice_queda_creado(pg):
    _correr(pg)
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pg_indexes "
                    "WHERE schemaname='scintela' AND indexname='cliente_codigo_cli_unico'")
        assert cur.fetchone()[0] == 1


@pytest.mark.db
@pytest.mark.parametrize("codigo_nuevo", ["H01", "h01", " H01 ", "h01  "])
def test_el_indice_rechaza_un_codigo_repetido(pg, codigo_nuevo):
    """Incluso escrito distinto: minúscula y espacios son el mismo código."""
    import psycopg2
    _correr(pg)
    with pytest.raises(psycopg2.errors.UniqueViolation), pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (60001, %s, 'INTRUSO')", (codigo_nuevo,))
    pg.rollback()


@pytest.mark.db
def test_el_indice_deja_pasar_un_codigo_libre(pg):
    _correr(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (60002, 'ZZQ', 'CLIENTE NUEVO')")
    pg.commit()
    assert _existe(pg, 60002)


@pytest.mark.db
def test_los_7_pares_abiertos_siguen_pudiendo_convivir(pg):
    _correr(pg)
    with pg.cursor() as cur:
        for _id_ex, _id_herm, cod in PARES_ABIERTOS:
            cur.execute("SELECT COUNT(*) FROM scintela.cliente "
                        "WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))
            assert cur.fetchone()[0] == 2, f"{cod} tenía que quedar duplicado"


@pytest.mark.db
def test_los_codigos_vacios_no_chocan_entre_si(pg):
    """Dos legacy sin código conviven, y se puede agregar un tercero."""
    _correr(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (60003, '', 'LEGACY TRES')")
    pg.commit()
    assert _existe(pg, 60003)


# --- Los tres modos de falla ------------------------------------------------
# En los tres el resultado esperado es el MISMO: excepción con el número de
# ficha, y CERO filas borradas.

@pytest.mark.db
def test_no_borra_nada_si_una_ficha_junto_un_movimiento(pg):
    import psycopg2
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.factura (codigo_cli) VALUES ('ALP')")
    pg.commit()
    antes = _n_clientes(pg)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "29243" in str(exc.value)
    assert _n_clientes(pg) == antes, "una sola transacción: o todas o ninguna"


@pytest.mark.db
def test_no_borra_nada_si_una_ficha_se_quedo_sin_hermana(pg):
    import psycopg2
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente = 90001")  # la de 29347
    pg.commit()
    antes = _n_clientes(pg)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "29347" in str(exc.value)
    assert _n_clientes(pg) == antes


@pytest.mark.db
def test_no_borra_nada_si_una_ficha_tiene_una_proforma(pg):
    import psycopg2
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.proforma_cabecera (id_cliente) VALUES (31346)")
    pg.commit()
    antes = _n_clientes(pg)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "31346" in str(exc.value)
    assert _n_clientes(pg) == antes


@pytest.mark.db
def test_una_hermana_condenada_no_alcanza_para_salvar(pg):
    """El caso que haría desaparecer al cliente entero.

    Si las dos únicas fichas de un RUC están las dos condenadas, se avalarían
    entre ellas y el cliente desaparecería entero. Tiene que romper — y tiene
    que nombrar a las dos.
    """
    import psycopg2
    with pg.cursor() as cur:
        # 29243 y 29347 se quedan sin sus hermanas buenas y pasan a compartir
        # RUC entre ellas. Son las dos de la lista: nadie las salva.
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente IN (90000, 90001)")
        cur.execute("UPDATE scintela.cliente SET ruc = '1177777777001' "
                    "WHERE id_cliente IN (29243, 29347)")
    pg.commit()
    antes = _n_clientes(pg)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "29243" in str(exc.value)
    assert "29347" in str(exc.value)
    assert _n_clientes(pg) == antes
