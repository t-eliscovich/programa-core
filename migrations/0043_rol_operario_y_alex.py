"""Seed del rol 'Operario' y migrar a Alex al rol nuevo.

Contexto: la migración 0042 corrió antes de que existiera el rol 'Operario'
y dejó al usuario 'alex' con rol 'Administrador'. Pedido dueña 2026-05-21:
Alex debe ver/editar todo MENOS Informes y todo lo que se accede desde ahí.

Esta migración:
  1) Lee `config/roles.py` y asegura que 'Operario' exista en seguridad.rol
     con TODOS sus permisos sincronizados (DELETE + INSERT). Idempotente.
  2) Si el usuario 'alex' tiene rol 'Administrador' (heredado de 0042),
     lo baja a 'Operario'. Si ya está en otro rol (porque la dueña lo
     cambió manualmente), no toca nada.

NOTA: para que el seed de Operario se mantenga en sync con cambios futuros
en config/roles.py, hay que re-correr la migración 0003 con --force, o
bien repetir el patrón de DELETE+INSERT de esta misma migración.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.roles import ROLES  # noqa: E402


def run(conn) -> None:
    cur = conn.cursor()

    # 1) Buscar el rol Operario en config/roles.py
    operario_perms = None
    for nombre, perms in ROLES:
        if nombre == "Operario":
            operario_perms = perms
            break
    if operario_perms is None:
        raise RuntimeError("Rol 'Operario' no está en config/roles.py — agregarlo primero.")

    # 2) Upsert rol + refrescar permisos.
    cur.execute(
        """
        INSERT INTO seguridad.rol (nombre_rol)
        VALUES (%s)
        ON CONFLICT (nombre_rol) DO UPDATE
            SET nombre_rol = EXCLUDED.nombre_rol
        RETURNING id_rol
        """,
        ("Operario",),
    )
    id_rol_operario = cur.fetchone()[0]

    cur.execute("DELETE FROM seguridad.permiso WHERE id_rol = %s", (id_rol_operario,))
    if operario_perms:
        cur.executemany(
            "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
            [(id_rol_operario, p) for p in operario_perms],
        )

    # 3) Migrar alex de Administrador → Operario (si aplica).
    cur.execute(
        """
        UPDATE seguridad.usuario u
           SET id_rol = %s
         WHERE lower(u.username) = 'alex'
           AND u.id_rol = (
               SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Administrador'
           )
        """,
        (id_rol_operario,),
    )
    n_migrados = cur.rowcount

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    rol 'Operario' sincronizado con {len(operario_perms)} permisos")
        if n_migrados:
            print(f"    alex bajado de Administrador → Operario ({n_migrados} fila)")
