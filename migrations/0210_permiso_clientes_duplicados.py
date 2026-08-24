"""Permiso propio `clientes.duplicados` — la pantalla de codigos repetidos.

TMT 2026-08-24 (duena): *"resolver los 7 codigos de cliente duplicados por las
pantallas. ponelo como alarma en programa core. y ahora le digo a alex"*.

El aviso nuevo del health lleva a `/admin/clientes-asinfo`, que es la pantalla
que dice que hacer con cada par. Colgaba de `admin_dbase.ver`, un permiso que
—medido contra `config/roles.py`— **no lo tiene ningun rol**: entraban solo
Accionista y Administrador por el wildcard `*`. Alex es INT, o sea que no
podia abrirla, y la campanita (fail-closed a proposito) ni siquiera le
mostraba el aviso. Una alarma que no le llega al que opera no es una alarma.

Darle `admin_dbase.ver` a INT le abriria TODO el panel de administracion
—health, importaciones sin plata, debug de grupos y partidas, deploy—. Por eso
la pantalla pasa a tener permiso PROPIO: se gatea por OPERACION, no por rol.

Quien lo recibe: **INT** (Alex, Irene, Maribel). Accionista y Administrador ya
lo tienen por el wildcard. Nadie pierde acceso: el permiso viejo no estaba en
ningun rol, asi que este cambio solo SUMA.

Ojo: la pantalla es de SOLO LECTURA. El que la abre no borra ni renombra nada
desde ahi — eso sigue pasando por `/clientes` y `/clientes/<id>/cambiar-codigo`,
que ya piden sus propios permisos.

`config/roles.py` es la fuente canonica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migracion el permiso existe solo en el repo,
`tiene_permiso()` da False para todos, y la pantalla queda cerrada hasta para
la duena — con los tests en verde (leccion migs 0164/0165, repetida en la
0182, la 0205, la 0207 y la 0208).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "clientes.duplicados"
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
