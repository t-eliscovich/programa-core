"""Comisiones y Metas: sólo Accionista y Administrador.

TMT 2026-08-05 (dueña): *"podés dejar que comisiones y metas no lo vean
usuarios INTELA"*, y al preguntarle el alcance eligió que las vean **sólo
Accionista y Administrador**. Sueldos y metas del equipo comercial no son
dato operativo.

⚠ POR QUÉ HACE FALTA ESTA MIGRACIÓN: `config/roles.py` es la fuente canónica,
pero el que manda en runtime es `seguridad.permiso` en la base —
`auth.load_logged_in_user` hace `SELECT nombre_opcion FROM seguridad.permiso
WHERE id_rol = %s`. Editar `roles.py` y no correr nada deja la producción
EXACTAMENTE IGUAL, y el código pasa los tests. Es el mismo camino que abrió la
0044 cuando los agregó.

Se BORRAN `comisiones.ver` y `metas.editar` de todos los roles. Los wildcard
(`*`) no los necesitan: `auth.tiene_permiso` y `requiere_permiso` chequean el
comodín aparte, así que Accionista y Administrador siguen entrando.

Revierte dos decisiones anteriores de ella, las dos deliberadas:
  · 2026-05-21 — `comisiones.ver` al rol operativo (migración 0044).
  · 2026-08-03 — `metas.editar` a Gerente.

⭐ NO toca el portal del vendedor: `/mi-cartera/comision` cuelga de
`micartera.ver`. Cada vendedor sigue viendo la suya.

Idempotente: correrla dos veces borra cero filas la segunda vez.
"""

from __future__ import annotations

import os

PERMISOS = ("comisiones.ver", "metas.editar")


def run(conn) -> None:
    cur = conn.cursor()

    # Con qué roles nos quedamos, para el log: los que TIENEN el permiso hoy.
    cur.execute(
        """
        SELECT r.nombre_rol, p.nombre_opcion
          FROM seguridad.permiso p
          JOIN seguridad.rol r ON r.id_rol = p.id_rol
         WHERE p.nombre_opcion = ANY(%s)
         ORDER BY 1, 2
        """,
        (list(PERMISOS),),
    )
    antes = cur.fetchall()

    cur.execute(
        "DELETE FROM seguridad.permiso WHERE nombre_opcion = ANY(%s)",
        (list(PERMISOS),),
    )
    borrados = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        for rol, permiso in antes:
            print(f"    − {permiso} de {rol}")
        print(f"    {borrados} permiso(s) borrado(s); quedan sólo los wildcard")
