"""`tejeduria.ver`: la pantalla de Tejeduría Asinfo deja de pedir `compras.ver`.

TMT 2026-08-05 (dueña): *"INT sí tiene que poder ver todo lo de stock,
tejeduría, Asinfo también"* — el mismo día en que le sacamos `compras.*` (mig
0166).

El choque: `/produccion-tejeduria-asinfo` pedía `compras.ver` porque cada OF de
tejeduría CREA una compra. Pero eso es de dónde salen los datos, no quién
debería mirarlos: ver la producción por tejedor (kilos, tarifas, OFs) no es lo
mismo que ver el libro de compras de la empresa. Atadas al mismo permiso, había
que elegir entre darle las dos a INT o ninguna.

Esta migración:
  1) `tejeduria.ver` a TODOS los roles que hoy tienen `compras.ver` — Gerente,
     Contabilidad, Compras, Bodega y Lectura. Para ellos no cambia nada: veían
     la pantalla y la siguen viendo. Sin este paso, cambiar el decorador les
     cortaba el acceso (mismo error que la 0044 evitó en su momento).
  2) `tejeduria.ver` a INT, que es el pedido.

LO QUE ESCRIBE no se toca: cargar tejeduría sigue pidiendo `compras.crear` y
las tarifas `tarifas.editar`. INT mira, no carga.

Los wildcard (Accionista, Administrador) no lo necesitan explícito.

Idempotente (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import os

PERMISO = "tejeduria.ver"


def run(conn) -> None:
    cur = conn.cursor()

    # 1) Todos los que ya entraban por compras.ver.
    cur.execute(
        """
        INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
        SELECT DISTINCT id_rol, %s
          FROM seguridad.permiso
         WHERE nombre_opcion = 'compras.ver'
        ON CONFLICT (id_rol, nombre_opcion) DO NOTHING
        """,
        (PERMISO,),
    )
    heredados = cur.rowcount

    # 2) Y el rol INT, que perdió compras.ver en la 0166.
    cur.execute(
        """
        INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
        SELECT r.id_rol, %s
          FROM seguridad.rol r
         WHERE r.nombre_rol = 'INT'
        ON CONFLICT (id_rol, nombre_opcion) DO NOTHING
        """,
        (PERMISO,),
    )
    int_ = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    {PERMISO}: {heredados} rol(es) que ya veían compras + {int_} (INT)")
