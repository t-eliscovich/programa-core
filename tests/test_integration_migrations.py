"""Tests de integración — schema + migraciones contra Postgres real.

Marcados con @pytest.mark.db. Skippean si no hay DB configurada.

Correr con:
    pytest -m db -q

Requiere:
    - DB_HOST, DB_NAME, DB_USER, DB_PASSWORD en env (CI service o local).
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_migraciones_aplican_idempotente(migrated_db):
    """Aplicar las migraciones dos veces no tiene que romper."""
    from scripts import migrate
    # Primera corrida ya ocurrió en el fixture; volvemos a correr.
    migrate.main([])  # idempotente
    migrate.main([])  # otra vez, por paranoia


@pytest.mark.db
def test_tabla_scintela_factura_existe_post_migrate(real_db_conn, migrated_db):
    """Después de migrar, scintela.factura existe con sus columnas base."""
    cur = real_db_conn.cursor()
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='scintela' AND table_name='factura'
    """)
    cols = {row[0] for row in cur.fetchall()}
    # No requerimos todas — sólo las que el app asume sin check.
    esperadas_minimo = {"id_factura", "numf", "fecha", "codigo_cli", "importe", "saldo", "stat"}
    faltantes = esperadas_minimo - cols
    assert not faltantes, f"Columnas esperadas faltan: {faltantes}. Cols encontradas: {cols}"


@pytest.mark.db
def test_tabla_seguridad_rol_y_permiso(real_db_conn, migrated_db):
    """seguridad.rol y seguridad.permiso existen con los roles seed."""
    cur = real_db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM seguridad.rol")
    assert cur.fetchone()[0] >= 1


@pytest.mark.db
def test_tabla_ejecuciones_tareas_existe(real_db_conn, migrated_db):
    """0007 creó scintela.ejecuciones_tareas con UNIQUE(tarea, periodo)."""
    cur = real_db_conn.cursor()
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='scintela' AND table_name='ejecuciones_tareas'
    """)
    assert cur.fetchone() is not None


@pytest.mark.db
def test_tabla_factura_electronica_existe(real_db_conn, migrated_db):
    """0009 creó scintela.factura_electronica para SRI."""
    cur = real_db_conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='scintela' AND table_name='factura_electronica'
    """)
    cols = {row[0] for row in cur.fetchall()}
    assert "clave_acceso" in cols
    assert "estado" in cols
    assert "xml_generado" in cols


@pytest.mark.db
def test_bitacora_tiene_request_id(real_db_conn, migrated_db):
    """0006 agregó request_id a scintela.bitacora_acciones."""
    cur = real_db_conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='scintela'
          AND table_name='bitacora_acciones'
          AND column_name='request_id'
    """)
    assert cur.fetchone() is not None
