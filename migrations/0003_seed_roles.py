"""Seed roles + permisos from the canonical config/roles.py.

Runs inside the migration runner — it receives an open psycopg2 connection
with autocommit OFF and must leave it in a committable state. Idempotent:
rows are upserted, permisos are refreshed (DELETE + INSERT) so editing
config/roles.py and re-running this migration (via `--force 0003`) keeps the
DB in sync.

Note: this does NOT create any user. The first owner user is created
separately via `scripts/seed_roles.py` (interactive) or env vars when the app
launches for the first time. That's intentional — users are environment-
specific, roles are not.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the project root importable so we can pull config/roles.py.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.roles import ROLES  # noqa: E402


def run(conn) -> None:
    cur = conn.cursor()

    # Tables may already exist (the dump ships them). IF NOT EXISTS keeps
    # this migration safe to run on a fresh DB OR on top of the legacy dump.
    cur.execute("""
        CREATE SCHEMA IF NOT EXISTS seguridad;

        CREATE TABLE IF NOT EXISTS seguridad.rol (
            id_rol     serial PRIMARY KEY,
            nombre_rol varchar(40) UNIQUE NOT NULL,
            descripcion text
        );

        CREATE TABLE IF NOT EXISTS seguridad.usuario (
            id_usuario     serial PRIMARY KEY,
            username       varchar(40) UNIQUE NOT NULL,
            nombre_completo varchar(100),
            password_hash  varchar(100) NOT NULL,
            id_rol         int NOT NULL REFERENCES seguridad.rol(id_rol),
            activo         boolean NOT NULL DEFAULT TRUE,
            fecha_crea     timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
            fecha_modifica timestamp
        );

        CREATE TABLE IF NOT EXISTS seguridad.permiso (
            id_permiso    serial PRIMARY KEY,
            id_rol        int NOT NULL REFERENCES seguridad.rol(id_rol) ON DELETE CASCADE,
            nombre_opcion varchar(80) NOT NULL,
            UNIQUE (id_rol, nombre_opcion)
        );
    """)

    for nombre_rol, permisos in ROLES:
        # Upsert rol. If it already exists, just grab its id.
        cur.execute(
            """
            INSERT INTO seguridad.rol (nombre_rol)
            VALUES (%s)
            ON CONFLICT (nombre_rol) DO UPDATE
                SET nombre_rol = EXCLUDED.nombre_rol
            RETURNING id_rol
            """,
            (nombre_rol,),
        )
        id_rol = cur.fetchone()[0]

        # Replace permisos: DELETE + INSERT is simpler than diffing and the
        # tables are tiny (a few hundred rows max).
        cur.execute(
            "DELETE FROM seguridad.permiso WHERE id_rol = %s",
            (id_rol,),
        )
        if permisos:
            # executemany is fine here — 20 perms per role, 6 roles.
            cur.executemany(
                "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
                [(id_rol, p) for p in permisos],
            )

    cur.close()
    # NO commit here — the runner commits on success.
    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    seeded {len(ROLES)} roles")
