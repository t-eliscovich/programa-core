"""Seed de los permisos del rol 'Alex' y migrar a alex al rol correcto si aplica.

Pedido dueña 2026-05-21/22: Alex debe ver/editar todo MENOS Informes y todo
lo que se accede desde ahí. La dueña pidió que el rol se llame literalmente
'Alex' (no 'Operario').

Esta migración:
  1) Lee `config/roles.py` y asegura que el rol 'Alex' exista en
     seguridad.rol con TODOS sus permisos sincronizados (DELETE + INSERT).
     Idempotente. El rol vacío puede haberlo creado 0042; acá le pegamos
     los permisos definitivos.
  2) Si el usuario 'alex' tiene rol 'Administrador' (por una corrida previa
     de 0042 antes de existir el rol 'Alex'), lo baja a 'Alex'. Si ya está
     en otro rol porque la dueña lo cambió manualmente, no toca nada.

NOTA: para que el seed de 'Alex' se mantenga en sync con cambios futuros en
config/roles.py, hay que re-correr la migración 0003 con --force, o bien
repetir el patrón de DELETE+INSERT de esta misma migración.
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

    # 1) Buscar el rol 'Alex' en config/roles.py
    alex_perms = None
    for nombre, perms in ROLES:
        if nombre in ("Alex", "INT"):  # 'Alex' fue renombrado a 'INT' (dueña 2026-07-08); es el mismo rol/permisos
            alex_perms = perms
            break
    if alex_perms is None:
        raise RuntimeError("Rol 'Alex' no está en config/roles.py — agregarlo primero.")

    # 2) Upsert rol + refrescar permisos.
    cur.execute(
        """
        INSERT INTO seguridad.rol (nombre_rol)
        VALUES (%s)
        ON CONFLICT (nombre_rol) DO UPDATE
            SET nombre_rol = EXCLUDED.nombre_rol
        RETURNING id_rol
        """,
        ("Alex",),
    )
    id_rol_alex = cur.fetchone()[0]

    cur.execute("DELETE FROM seguridad.permiso WHERE id_rol = %s", (id_rol_alex,))
    if alex_perms:
        cur.executemany(
            "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
            [(id_rol_alex, p) for p in alex_perms],
        )

    # 3) Migrar alex de Administrador → Alex (si quedó en Administrador por
    #    una corrida previa donde el rol 'Alex' no existía).
    cur.execute(
        """
        UPDATE seguridad.usuario u
           SET id_rol = %s
         WHERE lower(u.username) = 'alex'
           AND u.id_rol = (
               SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Administrador'
           )
        """,
        (id_rol_alex,),
    )
    n_migrados = cur.rowcount

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    rol 'Alex' sincronizado con {len(alex_perms)} permisos")
        if n_migrados:
            print(f"    alex bajado de Administrador → Alex ({n_migrados} fila)")
