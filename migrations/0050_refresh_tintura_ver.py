"""Refrescar el permiso `tintura.ver` para los roles que ahora lo necesitan.

Pedido dueña 2026-05-26: agregar sección Tintorería al sidebar visible
para Alex (no tenía informes.ver y por eso no veía la pantalla).

Ahora /informes/comparativa-tintoreria, /informes/tintoreria y
/stock/quimicos requieren `tintura.ver` (antes `informes.ver`).

Roles afectados (todos los que YA tenían informes.ver + Alex):
    - Administrador, Gerente, Contabilidad, Cobranzas → +tintura.ver
    - Alex → +tintura.ver
    - Accionista usa `*`/wildcard (no necesita grant explícito).
    - QC ya tenía tintura.ver.

Schema real (per 0003_seed_roles.py):
    seguridad.permiso(id_permiso, id_rol, nombre_opcion)
    UNIQUE (id_rol, nombre_opcion)

Idempotente: ON CONFLICT DO NOTHING en (id_rol, nombre_opcion).
"""
from __future__ import annotations

ROLES_NUEVOS = (
    "Administrador",
    "Gerente",
    "Contabilidad",
    "Cobranzas",
    "Alex",
)


def run(conn) -> None:
    cur = conn.cursor()

    grants_creados = 0
    for nombre_rol in ROLES_NUEVOS:
        cur.execute(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s",
            (nombre_rol,),
        )
        row = cur.fetchone()
        if not row:
            print(f"  WARN: rol '{nombre_rol}' no existe en seguridad.rol — skip")
            continue
        id_rol = row[0]
        cur.execute(
            """
            INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
            VALUES (%s, %s)
            ON CONFLICT (id_rol, nombre_opcion) DO NOTHING
            """,
            (id_rol, "tintura.ver"),
        )
        if cur.rowcount:
            grants_creados += 1
            print(f"  + {nombre_rol} -> tintura.ver")
        else:
            print(f"  = {nombre_rol} ya tenia tintura.ver")

    print(f"  Total grants nuevos: {grants_creados}")
