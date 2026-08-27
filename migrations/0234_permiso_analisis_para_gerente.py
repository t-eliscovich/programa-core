"""`analisis.ver` para Gerente — la otra mitad de lo que la 0215 dejo a medias.

TMT 2026-08-27 (duena): *"yo quiero que INT tenga acceso tambien"*, cuando la
alarma de permisos aviso que el archivo y la base decian cosas distintas.

Que habia pasado: el 2026-08-25 la decision fue abrirle Analisis a INT. La
migracion 0215 se lo dio a INT en la base (bien), pero en `config/roles.py` la
linea quedo escrita en la lista del rol Gerente (mal). Resultado: el repo le
daba el permiso a Gerente y la base a INT, y ninguno de los dos lados estaba
completo. La duena resolvio el empate hacia arriba: lo tienen los DOS.

Que abre para Gerente: la seccion Analisis entera -- Saldos tela por tela, la
hoja de a quien ofrecerle que, la lista impresa y el tablero de la Competencia.
Es informacion de fabrica: telas, colores, kilos y que cliente compro cada tela.
No hay plata adentro.

Lo que NO abre: /analisis/metas, que tiene su propio gate y decide como se
reparte la meta entre vendedores.

`config/roles.py` es la fuente canonica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migracion el permiso existe solo en el repo y
produccion queda identica, con los tests en verde (leccion migs 0164/0165,
repetida en la 0182, la 0205, la 0207 y la 0215).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "analisis.ver"
ROLES_QUE_LO_RECIBEN = ("Gerente",)


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
