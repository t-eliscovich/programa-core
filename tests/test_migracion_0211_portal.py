"""Las tres tablas del portal del cliente (mig 0211).

TMT 2026-08-24. Tres tablas y no una porque son tres cosas con vidas distintas:
el acceso dura para siempre, la bitácora crece sin parar y los códigos de
recuperación vencen a los 15 minutos.

Los estáticos corren siempre. Los de integración necesitan un Postgres
descartable —el mismo patrón que la mig 0155— y se prenden con `PG_PORTAL_DSN`:

    initdb -D /tmp/pgdata_portal -U pgtest --auth=trust
    pg_ctl -D /tmp/pgdata_portal -o '-p 5439' -l /tmp/pg.log start
    PG_PORTAL_DSN=postgresql://pgtest@127.0.0.1:5439/postgres \\
        pytest tests/test_migracion_0211_portal.py -q

⭐ Verificado así contra PostgreSQL 16.4 el 24/08: las tres tablas se crean, la
migración corre dos veces sin romper nada, y el índice único frena `' ATE '`
contra `'ate'`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SQL = (ROOT / "migrations" / "0211_portal_cliente.sql").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Estáticos
# ---------------------------------------------------------------------------


def test_estan_las_tres_tablas():
    for tabla in ("portal_acceso", "portal_ingreso", "portal_codigo"):
        assert f"CREATE TABLE IF NOT EXISTS scintela.{tabla}" in SQL


def test_el_usuario_del_portal_se_normaliza_como_joinea_el_sistema():
    """🚨 El usuario ES el código de 3 letras. Sin el UPPER(TRIM(...)) en el
    índice único, un espacio al final sería un usuario distinto — la misma
    lección que la mig 0155 y el índice de `cliente`."""
    assert ("CREATE UNIQUE INDEX IF NOT EXISTS portal_acceso_codigo_unico\n"
            "    ON scintela.portal_acceso (UPPER(TRIM(codigo_cli)))") in SQL


def test_todo_es_idempotente():
    """La migración puede correrse dos veces (reintento del deploy, o a mano
    desde /admin/migraciones)."""
    creates = [ln for ln in SQL.split("\n") if ln.startswith("CREATE ")]
    assert creates, "no hay ni un CREATE"
    for ln in creates:
        assert "IF NOT EXISTS" in ln, ln


def test_la_clave_y_el_codigo_van_CIFRADOS():
    """El que pueda leer estas tablas no tiene que poder entrar a la cuenta de
    nadie. Por eso son `_hash` y no el valor pelado."""
    assert "clave_hash" in SQL and "codigo_hash" in SQL
    for prohibido in ("clave text", "clave_plana", "codigo_plano"):
        assert prohibido not in SQL


def test_cortarle_el_acceso_no_borra_la_fila():
    """El vendedor corta el acceso desde Mi Cartera. Queda el rastro de que
    existió y de quién lo cortó — reversar no es eliminar."""
    for col in ("activo", "cortado_por", "cortado_en"):
        assert col in SQL


def test_el_mail_del_portal_no_es_el_de_la_ficha():
    """⚠ El portal NO pisa el maestro de clientes. Y de esta columna sale solo
    cuántos clientes cambiaron el mail, que es lo que la dueña quería medir."""
    assert "mail_cambiado" in SQL


def test_el_codigo_de_recuperacion_vence():
    assert "vence_en" in SQL and "usado_en" in SQL


# ---------------------------------------------------------------------------
# Contra un Postgres de verdad
# ---------------------------------------------------------------------------

_DSN = os.environ.get("PG_PORTAL_DSN")
sin_pg = pytest.mark.skipif(not _DSN, reason="sin PG_PORTAL_DSN")


@sin_pg
def test_corre_dos_veces_y_frena_el_duplicado():
    import psycopg2

    cn = psycopg2.connect(_DSN)
    cn.autocommit = True
    cur = cn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS scintela CASCADE; CREATE SCHEMA scintela")
    cur.execute(SQL)
    cur.execute(SQL)          # idempotente

    cur.execute("SELECT count(*) FROM information_schema.tables "
                " WHERE table_schema='scintela' AND table_name LIKE 'portal%'")
    assert cur.fetchone()[0] == 3

    cur.execute("INSERT INTO scintela.portal_acceso (codigo_cli) VALUES ('ate')")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO scintela.portal_acceso (codigo_cli) VALUES (' ATE ')")
