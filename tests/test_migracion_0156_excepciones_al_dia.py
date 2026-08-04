"""Migración 0156 — la lista de excepción del índice se pone al día sola.

La 0155 dejó 7 ids exceptuados, uno por cada par de códigos que todavía no
se había resuelto, y la regla escrita de que **la lista se achica, no crece**.
El paso manual —"cuando resuelvas uno, sacá su id con una migración nueva"—
es exactamente el que se olvida. La 0156 lo reemplaza por un recálculo.

Recalcular sólo es seguro porque el índice ya impide duplicados nuevos: el
conjunto de códigos repetidos sólo puede encoger. Estos tests fijan las dos
mitades de eso — que efectivamente encoja cuando se resuelven casos, y que
**corte sin tocar nada** si aparece un id que no estaba en la lista de la
0155, que sería la señal de que la premisa dejó de valer.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SQL_0155 = (RAIZ / "migrations" / "0155_cliente_codigo_sin_duplicados.sql").read_text(encoding="utf-8")
SQL_0156 = (RAIZ / "migrations" / "0156_cliente_codigo_excepciones_al_dia.sql").read_text(encoding="utf-8")

#: Los 7 que la 0155 dejó abiertos. La 0156 no puede exceptuar nada fuera de acá.
EXCEPTUADAS_0155 = {29142, 30547, 31469, 29048, 31038, 32066, 31562}


# ---------------------------------------------------------------------------
# Estáticos
# ---------------------------------------------------------------------------


def test_el_techo_de_la_0156_es_exactamente_la_lista_de_la_0155():
    """Si las dos listas se separan, el guard deja de guardar lo que dice."""
    m = re.search(r"v_0155\s+INTEGER\[\]\s*:=\s*ARRAY\s*\[(.*?)\]", SQL_0156, re.DOTALL)
    assert m, "No encontré la lista techo en la 0156."
    assert {int(n) for n in re.findall(r"\d+", m.group(1))} == EXCEPTUADAS_0155

    m2 = re.search(r"id_cliente\s+NOT\s+IN\s*\((.*?)\)", SQL_0155, re.DOTALL)
    assert {int(n) for n in re.findall(r"\d+", m2.group(1))} == EXCEPTUADAS_0155


def test_un_duplicado_nuevo_corta_en_vez_de_exceptuarse_solo():
    """La diferencia entre una lista que se achica y una puerta trasera."""
    assert "RAISE EXCEPTION" in SQL_0156
    assert "v_intrusos" in SQL_0156


def test_el_indice_recreado_sigue_normalizando_y_salteando_los_vacios():
    """Recalcular la excepción no puede aflojar el resto del candado."""
    for ddl in re.findall(r"CREATE UNIQUE INDEX.*?'", SQL_0156, re.DOTALL):
        assert "UPPER(TRIM(codigo_cli))" in ddl or "cliente_codigo_cli_unico" in ddl
    assert SQL_0156.count("UPPER(TRIM(codigo_cli))") >= 2, "las dos ramas indexan igual"
    assert SQL_0156.count("COALESCE(TRIM(codigo_cli)") >= 2, "las dos ramas saltean los vacíos"


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
    ruc         VARCHAR(20)
);
"""

#: Los 7 pares abiertos: (id exceptuado, id hermano indexado, código).
PARES = [
    (29142, 28929, "BLP"), (30547, 29114, "BRC"), (31469, 29893, "JQS"),
    (29048, 29008, "LEC"), (31038, 29995, "YMO"), (32066, 31088, "JRP"),
    (31562, 29170, "MTE"),
]

#: El índice tal cual lo dejó la 0155.
INDICE_0155 = """
CREATE UNIQUE INDEX cliente_codigo_cli_unico
    ON scintela.cliente (UPPER(TRIM(codigo_cli)))
 WHERE COALESCE(TRIM(codigo_cli), '') <> ''
   AND id_cliente NOT IN (29142, 30547, 31469, 29048, 31038, 32066, 31562);
"""


@pytest.fixture
def pg():
    """Postgres descartable con los 7 pares y el índice de la 0155 puestos."""
    dsn = os.environ.get("PG0155_DSN")
    if not dsn:
        pytest.skip("Sin PG0155_DSN: los tests de integración de la 0156 no corren.")
    if "test" not in dsn.rsplit("/", 1)[-1].lower():
        pytest.fail("PG0155_DSN tiene que apuntar a una base de TEST.")
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(ESQUEMA)
        for j, (id_ex, id_herm, cod) in enumerate(PARES):
            cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
                        "VALUES (%s, %s, %s, %s)", (id_ex, cod, f"{cod} SOBRANTE", f"{2200000000 + j}001"))
            cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc) "
                        "VALUES (%s, %s, %s, %s)", (id_herm, cod, f"{cod} QUEDA", f"{2300000000 + j}001"))
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (70001, '', 'LEGACY SIN CODIGO')")
        cur.execute(INDICE_0155)
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _correr(conn):
    with conn.cursor() as cur:
        cur.execute(SQL_0156)
    conn.commit()


def _indexdef(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='scintela' AND indexname='cliente_codigo_cli_unico'")
        fila = cur.fetchone()
    assert fila, "el índice tiene que existir después de correr la 0156"
    return fila[0]


def _excepciones(conn) -> set[int]:
    """Los ids que hoy quedan afuera del índice, leídos del índice REAL.

    Ojo: Postgres no guarda el `NOT IN (...)` tal cual — lo normaliza a
    `<> ALL (ARRAY[...])` al mostrar el `indexdef`. Buscar sólo "NOT IN" da
    siempre vacío y hace pasar cualquier cosa.
    """
    definicion = _indexdef(conn)
    m = re.search(r"<> ALL \(([^)]*)\)|NOT IN \(([^)]*)\)", definicion)
    if not m:
        return set()
    return {int(n) for n in re.findall(r"\d+", m.group(1) or m.group(2))}


@pytest.mark.db
def test_sin_resolver_nada_la_lista_queda_igual(pg):
    _correr(pg)
    assert _excepciones(pg) == EXCEPTUADAS_0155


@pytest.mark.db
def test_al_resolver_un_caso_su_id_sale_de_la_lista(pg):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente = 31469")  # JQS
    pg.commit()
    _correr(pg)
    assert _excepciones(pg) == EXCEPTUADAS_0155 - {31469}


@pytest.mark.db
def test_al_borrar_las_DOS_fichas_de_un_codigo_tambien_sale(pg):
    """El caso YMO: Alex dijo "eliminar los dos"."""
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente IN (31038, 29995)")
    pg.commit()
    _correr(pg)
    assert _excepciones(pg) == EXCEPTUADAS_0155 - {31038}


@pytest.mark.db
def test_resueltos_los_7_el_indice_se_queda_sin_excepciones(pg):
    """El estado final buscado: el candado cubre la tabla entera."""
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente = ANY(%s)",
                    (list(EXCEPTUADAS_0155),))
    pg.commit()
    _correr(pg)
    assert _excepciones(pg) == set()
    definicion = _indexdef(pg)
    assert "ALL" not in definicion and "NOT IN" not in definicion


@pytest.mark.db
def test_el_indice_sigue_prohibiendo_repetir_despues_de_recalcular(pg):
    import psycopg2
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente = ANY(%s)",
                    (list(EXCEPTUADAS_0155),))
    pg.commit()
    _correr(pg)
    with pytest.raises(psycopg2.errors.UniqueViolation), pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (60001, ' blp ', 'INTRUSO')")
    pg.rollback()


@pytest.mark.db
def test_los_codigos_vacios_siguen_conviviendo_con_excepciones_puestas(pg):
    """El mismo chequeo, pero por la rama CON lista de excepción.

    Las dos ramas construyen el DDL por separado, así que probar una sola
    deja la otra sin red. Es exactamente donde se coló el bug de las comillas
    (`<> ''''` en vez de `<> ''`): el predicado dejaba de filtrar los códigos
    vacíos y los legacy sin código empezaban a chocar entre ellos.
    """
    _correr(pg)
    assert _excepciones(pg) == EXCEPTUADAS_0155, "esta prueba tiene que ir por la rama con NOT IN"
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (70003, '', 'LEGACY TRES')")
    pg.commit()


@pytest.mark.db
def test_los_codigos_vacios_siguen_conviviendo(pg):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM scintela.cliente WHERE id_cliente = ANY(%s)",
                    (list(EXCEPTUADAS_0155),))
    pg.commit()
    _correr(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (70002, '', 'LEGACY DOS')")
    pg.commit()


@pytest.mark.db
def test_es_idempotente(pg):
    _correr(pg)
    primera = _excepciones(pg)
    _correr(pg)
    assert _excepciones(pg) == primera


@pytest.mark.db
def test_un_duplicado_nuevo_corta_y_no_toca_el_indice(pg):
    """⭐ Si el índice se cayó y entró un duplicado, la 0156 NO lo blanquea.

    Auto-exceptuarlo sería convertir el recálculo en una puerta trasera: el
    duplicado nuevo quedaría bendecido sin que nadie se enterara.
    """
    import psycopg2
    with pg.cursor() as cur:
        cur.execute("DROP INDEX scintela.cliente_codigo_cli_unico")
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (60002, 'ZZQ', 'UNO'), (60003, 'ZZQ', 'DOS')")
    pg.commit()
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "60003" in str(exc.value)
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pg_indexes "
                    "WHERE schemaname='scintela' AND indexname='cliente_codigo_cli_unico'")
        assert cur.fetchone()[0] == 0, "no recrea el índice a medias"
