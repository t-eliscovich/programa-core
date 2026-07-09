"""Refresh permisos: comisiones.ver / ventas.ver / gastos.ver granulares.

Las vistas de /comisiones, /informes/ventas-anio y /informes/gastos antes
exigían `informes.ver`. La dueña 2026-05-21 pidió que Alex (rol Alex)
las pueda ver SIN darle acceso a TODO el módulo Informes. Así que cambiamos
los decoradores a `comisiones.ver`, `ventas.ver`, `gastos.ver` respectivamente.

Esta migración:
  1) Refresca los permisos del rol 'Alex' desde config/roles.py
     (DELETE + INSERT). Tras esto Alex tiene los 3 permisos nuevos.
  2) Inserta `comisiones.ver` en TODOS los roles que ya tenían
     `informes.ver` — de lo contrario el cambio de decorador les corta
     el acceso (Administrador/Gerente/Contabilidad/Cobranzas/Lectura).
     Idempotente (ON CONFLICT DO NOTHING).
  3) Idem `ventas.ver` y `gastos.ver` — pero esos permisos YA existían
     en Administrador/Gerente/Contabilidad/Lectura desde antes. No tocamos.

El rol 'Accionista' tiene wildcard `*` que el código de auth chequea aparte,
así que no necesita el permiso explícito.
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

    # 1) Refrescar permisos del rol 'Alex' desde la fuente canónica.
    operario_perms = None
    for nombre, perms in ROLES:
        if nombre in ("Alex", "INT"):  # 'Alex' fue renombrado a 'INT' (dueña 2026-07-08); es el mismo rol/permisos
            operario_perms = perms
            break
    if operario_perms is None:
        raise RuntimeError("Rol 'Alex' no está en config/roles.py.")

    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Alex'")
    row = cur.fetchone()
    if not row:
        # Si el rol no existe todavía (migración 0043 no corrió), lo creamos.
        cur.execute(
            "INSERT INTO seguridad.rol (nombre_rol) VALUES (%s) RETURNING id_rol",
            ("Alex",),
        )
        row = cur.fetchone()
    id_rol_operario = row[0]

    cur.execute("DELETE FROM seguridad.permiso WHERE id_rol = %s", (id_rol_operario,))
    if operario_perms:
        cur.executemany(
            "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
            [(id_rol_operario, p) for p in operario_perms],
        )

    # 2) Asegurar comisiones.ver en todos los roles que tenían informes.ver.
    #    Es un INSERT idempotente por unique (id_rol, nombre_opcion).
    cur.execute(
        """
        INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
        SELECT DISTINCT id_rol, 'comisiones.ver'
          FROM seguridad.permiso
         WHERE nombre_opcion = 'informes.ver'
        ON CONFLICT (id_rol, nombre_opcion) DO NOTHING
        """
    )
    n_comisiones = cur.rowcount

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    Alex refrescado con {len(operario_perms)} permisos")
        print(f"    comisiones.ver insertado en {n_comisiones} rol(es)")
