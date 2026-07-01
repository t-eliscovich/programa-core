"""Refresh permisos del rol 'Alex' — review de accesos 2026-07-01 (dueña).

La duena pidio revisar a fondo donde a Alex lo frenan permisos que no le
corresponden (a raiz de que el reverso pedia informes.ver sin ser Informes).
Resultado: Alex ahora tambien OPERA (no solo ve) en varias pantallas que ya
usaba. Esta migracion refresca los permisos del rol 'Alex' desde la fuente
canonica config/roles.py (DELETE + INSERT), que ya incluye:

    + bancos.editar        (recalcular saldos de un banco)
    + gastos.crear         (clasificar caja->gasto / crear gasto)
    + gastos.editar        (clasificar / editar gasto)
    + gastos.anular        (anular / desclasificar gasto)
    + tintura.registrar    (cargar/editar tinto-carga y costos)

(El informe de Deudas NO necesita permiso nuevo: Alex ya tenia deudas.ver y
el decorador de /deudas ahora acepta deudas.ver ademas de informes.ver.)

Idempotente: DELETE + re-INSERT de TODO el set del rol 'Alex' desde roles.py.
El rol 'Accionista' usa wildcard '*' aparte, no lo tocamos.
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

    alex_perms = None
    for nombre, perms in ROLES:
        if nombre == "Alex":
            alex_perms = perms
            break
    if alex_perms is None:
        raise RuntimeError("Rol 'Alex' no esta en config/roles.py.")

    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Alex'")
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO seguridad.rol (nombre_rol) VALUES (%s) RETURNING id_rol",
            ("Alex",),
        )
        row = cur.fetchone()
    id_rol_alex = row[0]

    cur.execute("DELETE FROM seguridad.permiso WHERE id_rol = %s", (id_rol_alex,))
    cur.executemany(
        "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
        [(id_rol_alex, p) for p in alex_perms],
    )

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    Alex refrescado con {len(alex_perms)} permisos")
