"""Migración 0157 — el candado sin excepciones.

Cierre del tema: 20 códigos repetidos → 0. La 0157 saca el `NOT IN` del
índice, así que a partir de acá `scintela.cliente` está cubierta entera.

Lo único que importa probar es que **no lo saque a ciegas**: si quedara un
código repetido, sacar la excepción no lo arreglaría — dejaría el índice sin
poder crearse y, peor, si por algún camino se creara, estaría bendiciendo el
duplicado. El guard exige cero repetidos y, si no, corta sin tocar el índice
que ya está puesto.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SQL_0157 = (RAIZ / "migrations" / "0157_cliente_codigo_unico_sin_excepciones.sql").read_text(encoding="utf-8")


def _ddl() -> str:
    """El `CREATE UNIQUE INDEX`, sin comentarios.

    Mirar el archivo entero no sirve: el header explica que el índice va
    *sin* `NOT IN`, así que el string aparece igual y el test pasa siempre.
    """
    m = re.search(r"CREATE UNIQUE INDEX.*?;", SQL_0157, re.DOTALL)
    assert m, "No encontré el CREATE UNIQUE INDEX."
    return re.sub(r"--[^\n]*", "", m.group(0))


def test_el_indice_final_no_tiene_lista_de_excepcion():
    assert "NOT IN" not in _ddl()


def test_el_indice_final_sigue_normalizando_y_salteando_los_vacios():
    """Sacar la excepción no puede aflojar el resto del candado."""
    assert "UPPER(TRIM(codigo_cli))" in _ddl()
    assert "COALESCE(TRIM(codigo_cli), '') <> ''" in _ddl()


def test_no_saca_la_excepcion_si_todavia_queda_un_repetido():
    assert "RAISE EXCEPTION" in SQL_0157


# ---------------------------------------------------------------------------
# Integración
# ---------------------------------------------------------------------------

ESQUEMA = """
DROP SCHEMA IF EXISTS scintela CASCADE;
CREATE SCHEMA scintela;
CREATE TABLE scintela.cliente (
    id_cliente  INTEGER PRIMARY KEY,
    codigo_cli  VARCHAR(10),
    nombre      VARCHAR(200)
);
INSERT INTO scintela.cliente VALUES
    (28929, 'BLP', 'RAMIRO AYO'),
    (29114, 'BRC', 'BRAULIO CUENCA'),
    (29008, 'LEC', 'LUIS CANAMAR'),
    (70001, '',    'LEGACY UNO'),
    (70002, NULL,  'LEGACY DOS');

CREATE UNIQUE INDEX cliente_codigo_cli_unico
    ON scintela.cliente (UPPER(TRIM(codigo_cli)))
 WHERE COALESCE(TRIM(codigo_cli), '') <> ''
   AND id_cliente NOT IN (29048, 29142, 30547);
"""


@pytest.fixture
def pg():
    dsn = os.environ.get("PG0155_DSN")
    if not dsn:
        pytest.skip("Sin PG0155_DSN: los tests de integración de la 0157 no corren.")
    if "test" not in dsn.rsplit("/", 1)[-1].lower():
        pytest.fail("PG0155_DSN tiene que apuntar a una base de TEST.")
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(ESQUEMA)
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _correr(conn):
    with conn.cursor() as cur:
        cur.execute(SQL_0157)
    conn.commit()


def _indexdef(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='scintela' AND indexname='cliente_codigo_cli_unico'")
        fila = cur.fetchone()
    return fila[0] if fila else None


@pytest.mark.db
def test_sin_repetidos_el_indice_queda_sin_excepciones(pg):
    _correr(pg)
    definicion = _indexdef(pg)
    assert definicion and "ALL" not in definicion and "NOT IN" not in definicion


@pytest.mark.db
def test_es_idempotente(pg):
    _correr(pg)
    primera = _indexdef(pg)
    _correr(pg)
    assert _indexdef(pg) == primera


@pytest.mark.db
def test_ahora_cualquier_codigo_repetido_explota(pg):
    """Ni uno: ya no hay ficha exceptuada que pueda convivir duplicada."""
    import psycopg2
    _correr(pg)
    for id_nuevo, cod in ((60001, 'BLP'), (60002, ' blp '), (60003, 'LEC')):
        with pytest.raises(psycopg2.errors.UniqueViolation), pg.cursor() as cur:
            cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                        "VALUES (%s, %s, 'INTRUSO')", (id_nuevo, cod))
        pg.rollback()


@pytest.mark.db
def test_los_codigos_vacios_siguen_conviviendo(pg):
    _correr(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (70003, '', 'LEGACY TRES')")
    pg.commit()


@pytest.mark.db
def test_con_un_repetido_corta_y_deja_el_indice_viejo_en_pie(pg):
    """⭐ El caso que importa.

    Si todavía hay un duplicado, la 0157 no puede quitarle la protección a la
    tabla. Corta nombrando el código, y el índice de la 0156 —que es el que
    hoy está tapando el agujero— sigue existiendo.
    """
    import psycopg2
    with pg.cursor() as cur:
        # 29142 está en la lista de excepción del índice de la 0156, así que
        # este INSERT pasa: es exactamente el estado "queda uno abierto".
        cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre) "
                    "VALUES (29142, 'BLP', 'BLANCA')")
    pg.commit()
    antes = _indexdef(pg)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _correr(pg)
    pg.rollback()
    assert "BLP" in str(exc.value)
    assert _indexdef(pg) == antes, "el índice que protege hoy no se toca"
