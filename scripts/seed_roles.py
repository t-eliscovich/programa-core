"""Seed inicial de roles, permisos y primer usuario Dueño.

Este script ahora es una conveniencia: las tablas y los roles se cargan
con la migración 0003_seed_roles.py. Este archivo se queda únicamente
para crear el PRIMER usuario Dueño (paso interactivo o por env vars),
porque los usuarios son específicos del entorno y no deben vivir en
el pipeline de migraciones.

Orden recomendado:

    python scripts/migrate.py          # crea tablas + roles + permisos
    python scripts/seed_roles.py       # crea el primer usuario
"""
import getpass
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Fix path para imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt  # noqa: E402

import db  # noqa: E402
from config.roles import ROLES  # noqa: E402 — fuente canónica compartida con la migración


def ensure_tables():
    """Si las tablas de seguridad no existen, crearlas.

    Si YA existen (caso real en producción: el dump trae seguridad.* con
    id_permiso serial y usuario.nombre_completo), estos statements son no-op
    gracias a IF NOT EXISTS.
    """
    db.execute(
        """
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
        """
    )


def seed_roles():
    for nombre, permisos in ROLES:
        row = db.fetch_one(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s",
            (nombre,),
        )
        if row:
            id_rol = row["id_rol"]
            print(f"  · Rol {nombre!r} ya existe (id_rol={id_rol}).")
        else:
            inserted = db.execute_returning(
                "INSERT INTO seguridad.rol (nombre_rol) VALUES (%s) RETURNING id_rol",
                (nombre,),
            )
            id_rol = inserted["id_rol"]
            print(f"  ✓ Rol {nombre!r} creado (id_rol={id_rol}).")

        # Refrescar permisos (idempotente)
        db.execute("DELETE FROM seguridad.permiso WHERE id_rol = %s", (id_rol,))
        for p in permisos:
            db.execute(
                "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
                (id_rol, p),
            )
        print(f"    → {len(permisos)} permisos asignados.")


def crear_primer_dueno():
    existe = db.fetch_one("SELECT COUNT(*) AS n FROM seguridad.usuario")
    if existe and existe["n"] > 0:
        print("Ya hay usuarios. No creo uno nuevo.")
        return

    # 1) env vars — para uso no interactivo (launcher.sh)
    env_user = (os.environ.get("INTELA_ADMIN_USER") or "").strip().lower()
    env_pw   = os.environ.get("INTELA_ADMIN_PASSWORD") or ""

    if env_user and env_pw:
        username, pw1 = env_user, env_pw
        print(f"\nCreando primer usuario Dueño desde env: {username!r}")
    else:
        # 2) fallback interactivo
        print("\nCreando primer usuario Dueño:")
        username = input("  Usuario: ").strip().lower()
        if not username:
            print("Cancelado.")
            return
        while True:
            pw1 = getpass.getpass("  Contraseña: ")
            pw2 = getpass.getpass("  Repetir:    ")
            if pw1 and pw1 == pw2:
                break
            print("  No coinciden o vacía, probá de nuevo.")

    # TMT 2026-05-19 v8 — "Dueño" renombrado a "Accionista". Fallback al
    # nombre viejo si la migración 0035 no se corrió todavía.
    rol = (
        db.fetch_one("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Accionista'")
        or db.fetch_one("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Dueño'")
    )
    hashed = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.execute(
        """
        INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo)
        VALUES (%s, %s, %s, TRUE)
        """,
        (username, hashed, rol["id_rol"]),
    )
    print(f"  ✓ Usuario {username!r} creado con rol Accionista.")


def main():
    print("=== Seed inicial Programa Core ===\n")
    print("1) Asegurando tablas de seguridad…")
    ensure_tables()
    print("2) Cargando roles + permisos…")
    seed_roles()
    print("3) Primer usuario…")
    crear_primer_dueno()
    print("\nListo.")


if __name__ == "__main__":
    main()
