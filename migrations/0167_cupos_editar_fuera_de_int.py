"""`cupos.editar` sale del rol INT: el cupo lo tocan sólo Andrés y accionistas.

TMT 2026-08-05 (dueña): *"sí, sacales cupos"*.

⭐ ESTO NO ES UNA DECISIÓN NUEVA — es terminar una del 2026-07-09. La migración
0123 dice textual *"solo andres y accionistas pueden editar cupo de clientes"*
y le sacó `cupos.editar` a Cobranzas, Ventas y QC… pero **no a INT**, que en
ese momento se llamaba "Alex". Quedó como olvido: el permiso siguió en la base
un mes, mientras `config/roles.py` —que es la fuente canónica— ya no lo listaba
para ese rol.

Lo destapó `/usuarios/accesos` (2026-08-05) al cruzar el repo con la base: el
rol tenía 35 permisos en producción y 34 en el archivo. Esa diferencia de uno
era ésta.

⚠ Y es la moraleja de todo el día: el archivo no manda, la base manda. Un
permiso sacado de `roles.py` sin migración sigue vivo en producción, y nadie
se entera hasta que alguien lo cruza.

Idempotente: la segunda corrida borra cero filas.
"""

from __future__ import annotations

import os

PERMISO = "cupos.editar"


def run(conn) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        SELECT r.nombre_rol
          FROM seguridad.permiso p
          JOIN seguridad.rol r ON r.id_rol = p.id_rol
         WHERE p.nombre_opcion = %s
         ORDER BY 1
        """,
        (PERMISO,),
    )
    antes = [r[0] for r in cur.fetchall()]

    # De TODOS los roles: los wildcard (Accionista, Administrador) no lo
    # necesitan explícito, y ningún otro rol debería tenerlo desde la 0123.
    cur.execute(
        "DELETE FROM seguridad.permiso WHERE nombre_opcion = %s", (PERMISO,)
    )
    borrados = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        for rol in antes:
            print(f"    − {PERMISO} de {rol}")
        print(f"    {borrados} fila(s); el cupo queda sólo para los wildcard")
