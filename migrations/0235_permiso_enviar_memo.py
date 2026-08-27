"""`pedidos.enviar_memo` para INT — el botón "Enviar memo" de /pedidos.

TMT 2026-08-27 (dueña): los pedidos se mandan como MEMO a la fábrica (tab
Memos de formulas_app), desde /pedidos de la oficina y desde el portal de
vendedores. Este permiso gatea el POST de la oficina.

Por qué permiso propio y no `facturas.ver` (que ya gatea la pantalla):
enviar es una ACCIÓN con efecto en otra base, no una lectura. La lección es
la de la mig 0208: un Confirmar habilitado por un permiso de lectura.

Los vendedores NO lo necesitan: su botón vive en /mi-cartera, va por
`micartera.ver` y sólo sobre pedidos propios.

`config/roles.py` es la fuente canónica pero el que manda en runtime es
`seguridad.permiso` (lección migs 0164/0165 y sucesoras).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "pedidos.enviar_memo"
ROLES_QUE_LO_RECIBEN = ("INT",)


def run(conn) -> None:
    cur = conn.cursor()
    agregados, ya, sin_rol = [], [], []

    for nombre_rol in ROLES_QUE_LO_RECIBEN:
        cur.execute(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s", (nombre_rol,)
        )
        fila = cur.fetchone()
        if not fila:
            sin_rol.append(nombre_rol)
            continue
        id_rol = fila[0]

        cur.execute(
            "SELECT 1 FROM seguridad.permiso WHERE id_rol = %s AND nombre_opcion = %s",
            (id_rol, PERMISO),
        )
        if cur.fetchone():
            ya.append(nombre_rol)
            continue

        cur.execute(
            "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
            (id_rol, PERMISO),
        )
        agregados.append(nombre_rol)

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        for rol in agregados:
            print(f"    + {PERMISO} a {rol}")
        for rol in ya:
            print(f"    = {rol} ya lo tenia")
        for rol in sin_rol:
            print(f"    ! rol {rol} no existe en esta base — salteado")
