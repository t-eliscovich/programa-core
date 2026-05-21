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

# Mapeo username → (clave canónica, nombre_rol a asignar si lo creamos de cero)
# TMT 2026-05-21 dueña: Alex va a 'Operario' (ver pantallas operativas pero
# NO Informes). Andres queda en 'Administrador'. Federico/Tamara son dueños
# (rol Accionista). Si el usuario ya existe, NO le pisamos el rol — sólo
# asignamos la clave.
USUARIOS_CANONICOS = [
    ("federico", "FED", "Accionista"),
    ("tamara", "TAM", "Accionista"),
    ("alex", "ALX", "Operario"),
    ("andres", "ADR", "Administrador"),
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

    # 2) Resolver id_rol por nombre. Cache en dict para no consultar
    #    repetido. Si el rol no existe (ej. 'Operario' antes de re-seed),
    #    levantamos error claro — la dueña debe correr antes 0003 --force.
    def _id_rol_por_nombre(nombre: str) -> int:
        cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s", (nombre,))
        r = cur.fetchone()
        if not r:
            raise RuntimeError(
                f"Rol {nombre!r} no existe en seguridad.rol. "
                f"Re-correr migración 0003 con --force para seedear roles nuevos."
            )
        return r[0]

    # 3) Para cada usuario canónico: si no existe lo crea, si existe le asigna clave.
    ph_default = bcrypt.hashpw(
        PASSWORD_PLACEHOLDER.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    creados, actualizados = [], []
    for username, clave, nombre_rol in USUARIOS_CANONICOS:
        id_rol = _id_rol_por_nombre(nombre_rol)
        cur.execute(
            "SELECT id_usuario FROM seguridad.usuario WHERE lower(username) = %s",
            (username,),
        )
        existing = cur.fetchone()
        if existing:
            # Solo asignar clave — no pisar password ni rol (la dueña puede
            # haber cambiado el rol manualmente).
            cur.execute(
                "UPDATE seguridad.usuario SET clave = %s WHERE id_usuario = %s",
                (clave, existing[0]),
            )
            actualizados.append((username, clave, nombre_rol))
        else:
            cur.execute(
                """
                INSERT INTO seguridad.usuario
                    (username, password_hash, id_rol, activo, clave)
                VALUES (%s, %s, %s, TRUE, %s)
                """,
                (username, ph_default, id_rol, clave),
            )
            creados.append((username, clave, nombre_rol))

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
