"""Permiso propio `estado_cuenta.totalizar` — el boton Confirmar del TOTALIZAR.

TMT 2026-08-20, auditando el totalizar. La ruta
`/informes/estado-cuenta/<cli>/totalizar` atiende GET y POST con el MISMO
gate, `clientes.ver`. El GET esta bien: es la pantalla de confirmacion y la ve
cualquiera que vea la cuenta. El POST no: redistribuye TODOS los abonos del
cliente y borra sus vinculos cheque<->factura.

`clientes.ver` es un permiso de LECTURA. Medido contra `seguridad.permiso` el
20/08, lo tienen ocho roles, y entre ellos **Lectura** y **Ventas**. Los dos
estan hoy en cero usuarios, asi que no hubo nadie suelto — pero el dia que se
de de alta a alguien "para que solo mire", ese alguien podria redistribuir la
cuenta de cualquier cliente. El vecino de al lado,
`estado_cuenta_neteo_deshacer`, ya pedia `cheques.anular`: la asimetria estaba
a la vista.

Quien lo recibe: **Gerente, Contabilidad, Cobranzas e INT**. Quien totaliza de
verdad, medido en `mov_doble`: alex (12 corridas desde el 07/07, la ultima el
20/08) y andres (1). Alex es INT, Andres es Accionista — los dos siguen
pudiendo. Accionista y Administrador ya lo tienen por el wildcard `*`.
Se quedan afuera Lectura y Ventas, que es justo el punto.

El mismo permiso gatea el DESHACER (`/estado-cuenta/totalizar/<id>/deshacer`),
que es POST-only: deshacer un totalizar mueve la misma plata que hacerlo.

🚨 Ojo al verificar: `/usuarios/accesos` SOLO lista rutas GET (`accesos.py`
filtra por `"GET" in regla.methods`), asi que la de deshacer no va a aparecer
ahi. Que no figure no quiere decir que la migracion no corrio.

`config/roles.py` es la fuente canonica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migracion el permiso existe solo en el repo,
`tiene_permiso()` da False para todos y el boton Confirmar desaparece hasta
para Alex — con los tests en verde (leccion migs 0164/0165, repetida en la
0182, la 0205 y la 0207).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "estado_cuenta.totalizar"
ROLES_QUE_LO_RECIBEN = ("Gerente", "Contabilidad", "Cobranzas", "INT")


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
