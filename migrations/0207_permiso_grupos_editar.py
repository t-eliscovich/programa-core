"""Permiso nuevo `grupos.editar` — la columna Grupo de /clientes.

TMT 2026-08-19 (duena, con las fotos del cuaderno de la oficina): *"Estos
clientes son de un mismo grupo. en clientes una columna mas que diga grupo y
ponemos el primero codigo que aparece en lista. Esto tiene que ser editable"*.

Que abre: el lapicito de la columna Grupo en `/clientes`, el campo Grupo de la
ficha del cliente y la carga masiva `/clientes/grupos-carga`.

Se lo damos a **INT** (Alex, Irene, Maribel) y **Cobranzas**: agrupar clientes
es una decision de cobranza -- quien paga por quien -- y son ellos los que
imprimen los estados de cuenta por grupo. Accionista y Administrador ya lo
tienen por el wildcard `*`.

Por que un permiso propio y no `clientes.editar`: el grupo no cambia un dato
del cliente, cambia COMO SE SUMAN LOS SALDOS. Mover a un cliente de grupo mueve
plata de una hoja impresa a otra y cambia el total de la cartera consolidada
(`/cartera/grupos`, `/informes/estado-cuenta/grupos`). Ese peso no es el de
corregir un telefono, y con permiso propio la duena puede cerrarlo despues sin
tener que cerrar toda la ficha del cliente.

`config/roles.py` es la fuente canonica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migracion el permiso existe solo en el repo y
produccion queda identica, con los tests en verde (leccion migs 0164/0165,
repetida en la 0182 y en la 0205).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "grupos.editar"
ROLES_QUE_LO_RECIBEN = ("INT", "Cobranzas")


def run(conn) -> None:
    cur = conn.cursor()
    agregados, ya, sin_rol = [], [], []

    for nombre_rol in ROLES_QUE_LO_RECIBEN:
        cur.execute(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s", (nombre_rol,)
        )
        fila = cur.fetchone()
        if not fila:
            # No se crea el rol: si no existe en esta base, es una base de
            # prueba o el rol se renombro. Mejor dejar constancia que inventar.
            sin_rol.append(nombre_rol)
            continue
        id_rol = fila[0]

        cur.execute(
            "SELECT 1 FROM seguridad.permiso "
            " WHERE id_rol = %s AND nombre_opcion = %s",
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
