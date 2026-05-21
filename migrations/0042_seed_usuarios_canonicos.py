"""Seed de los 4 usuarios canónicos con su clave de 3 letras.

Pedido dueña 2026-05-21: "Nombres FED TAM ALE AND. Agregar dos usuarios mas.
Alex y Andres". Aclaración posterior: el set canónico final son 4 usuarios:
Federico (FED), Tamara (TAM), Alex (ALX), Andres (ADR). ALE no es válida.

Esta migración:
  1) Asegura que los usernames `federico`, `tamara`, `alex`, `andres`
     existan en `seguridad.usuario`. Si no existen, los crea con un
     password placeholder (`Cambiar2026`) y rol "Administrador". La dueña
     debe avisarles para que lo cambien en su primer login (/usuarios o
     /me según el flujo).
  2) Asigna la clave de 3 letras a cada uno (UPDATE u SET clave=...).
  3) Si alguno ya existe con otro password/rol, NO los pisa — solo asigna
     clave. Idempotente: re-ejecutar no rompe nada.

Lo NO hace:
  - No borra usuarios existentes.
  - No cambia el password de usuarios ya creados.
  - No agrega clave a usuarios no listados (FED/TAM/ALX/ADR son los 4).
"""

from __future__ import annotations

import os

import bcrypt

# Mapeo username → (clave canónica, password placeholder si crea de cero)
USUARIOS_CANONICOS = [
    ("federico", "FED"),
    ("tamara", "TAM"),
    ("alex", "ALX"),
    ("andres", "ADR"),
]

PASSWORD_PLACEHOLDER = "Cambiar2026"  # los nuevos usuarios deben cambiarlo


def run(conn) -> None:
    cur = conn.cursor()

    # 1) Asegurar que la columna `clave` exista en seguridad.usuario.
    #    En entornos legacy (creación de la tabla anterior a 2026-05) podría
    #    no estar. ALTER IF NOT EXISTS lo deja siempre presente.
    cur.execute("""
        ALTER TABLE seguridad.usuario
            ADD COLUMN IF NOT EXISTS clave varchar(3)
    """)

    # 2) Buscar id del rol "Administrador" — fallback al primer rol no-Accionista
    #    si no existe (no debería pasar, pero por si la canónica cambió).
    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s", ("Administrador",))
    row = cur.fetchone()
    if row:
        id_rol_default = row[0]
    else:
        cur.execute(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol != 'Accionista' ORDER BY id_rol LIMIT 1"
        )
        row = cur.fetchone()
        id_rol_default = row[0] if row else None
    if id_rol_default is None:
        raise RuntimeError("No hay roles en seguridad.rol — corré antes la migración 0003_seed_roles.")

    # 3) Para cada usuario canónico: si no existe lo crea, si existe le asigna clave.
    ph_default = bcrypt.hashpw(
        PASSWORD_PLACEHOLDER.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    creados, actualizados = [], []
    for username, clave in USUARIOS_CANONICOS:
        cur.execute(
            "SELECT id_usuario FROM seguridad.usuario WHERE lower(username) = %s",
            (username,),
        )
        existing = cur.fetchone()
        if existing:
            # Solo asignar clave — no pisar password ni rol.
            cur.execute(
                "UPDATE seguridad.usuario SET clave = %s WHERE id_usuario = %s",
                (clave, existing[0]),
            )
            actualizados.append((username, clave))
        else:
            cur.execute(
                """
                INSERT INTO seguridad.usuario
                    (username, password_hash, id_rol, activo, clave)
                VALUES (%s, %s, %s, TRUE, %s)
                """,
                (username, ph_default, id_rol_default, clave),
            )
            creados.append((username, clave))

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        if creados:
            print(f"    creados (password placeholder='{PASSWORD_PLACEHOLDER}'):")
            for u, c in creados:
                print(f"      • {u}  clave={c}")
        if actualizados:
            print("    clave asignada a:")
            for u, c in actualizados:
                print(f"      • {u}  clave={c}")
