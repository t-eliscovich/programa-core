"""Posdatados: sólo Accionista y Administrador.

TMT 2026-08-05 (dueña): *"posdatados también sólo andres y accionistas"*.
Andrés tiene el rol **Administrador**, que es wildcard, así que el pedido se
traduce a los mismos dos roles que Comisiones y Metas (mig 0164).

Se borran los CUATRO permisos —`posdat.ver`, `.crear`, `.editar`, `.anular`—
de todos los roles: Gerente, Contabilidad, Compras, INT y Lectura.

⚠ NO era sólo lectura: el rol **INT** (Alex, Irene, Maribel) tenía
`crear/editar/anular`, o sea que OPERABA posdatados. Se le preguntó
explícitamente antes de sacarlo y eligió cerrarlo del todo. De acá en más los
carga y los anula ella o Andrés.

⚠ Igual que la 0164: `config/roles.py` es la fuente canónica pero el que manda
en runtime es `seguridad.permiso`. Sin esta migración el cambio existe sólo en
el repo y producción queda idéntica, con los tests en verde.

Idempotente.
"""

from __future__ import annotations

import os

PERMISOS = ("posdat.ver", "posdat.crear", "posdat.editar", "posdat.anular")


def run(conn) -> None:
    cur = conn.cursor()

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
