"""Refrescar el permiso `tintura.ver` para los roles que ahora lo necesitan.

Pedido dueña 2026-05-26: agregar sección Tintorería al sidebar visible
para Alex (no tenía informes.ver y por eso no veía la pantalla).

Ahora /informes/comparativa-tintoreria, /informes/tintoreria y
/stock/quimicos requieren `tintura.ver` (antes `informes.ver`).

Roles afectados (todos los que YA tenían informes.ver + Alex):
    - Administrador, Gerente, Contabilidad, Cobranzas → +tintura.ver
    - Alex → +tintura.ver (no tenía informes.ver, ahora ve Tintorería sin
      ver el resto de Informes)
    - Accionista usa `*` (acceso total), no necesita el grant explícito.
    - QC ya tenía tintura.ver.

Idempotente: re-correrlo no duplica grants (UNIQUE(rol, permiso) en
seguridad.rol_permiso).
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

    # 1) Asegurar que el permiso existe en seguridad.permiso.
    cur.execute(
        """
        INSERT INTO seguridad.permiso (nombre_permiso, descripcion)
        VALUES (%s, %s)
        ON CONFLICT (nombre_permiso) DO NOTHING
        """,
        ("tintura.ver", "Ver pantallas de tintorería (comparativa PC vs "
                        "formulas_app y stock de químicos)."),
    )

    # 2) ID del permiso.
    cur.execute(
        "SELECT id_permiso FROM seguridad.permiso WHERE nombre_permiso = 'tintura.ver'"
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("No se pudo asegurar el permiso tintura.ver")
    id_permiso = row[0]

    # 3) Para cada rol nuevo, asegurar el grant.
    grants_creados = 0
    for nombre_rol in ROLES_NUEVOS:
        cur.execute(
            "SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s",
            (nombre_rol,),
        )
        rrow = cur.fetchone()
        if not rrow:
            print(f"  WARN: rol '{nombre_rol}' no existe en seguridad.rol — skip")
            continue
        id_rol = rrow[0]
        cur.execute(
            """
            INSERT INTO seguridad.rol_permiso (id_rol, id_permiso)
            VALUES (%s, %s)
            ON CONFLICT (id_rol, id_permiso) DO NOTHING
            """,
            (id_rol, id_permiso),
        )
        if cur.rowcount:
            grants_creados += 1
            print(f"  + {nombre_rol} -> tintura.ver")

    print(f"  Total grants nuevos: {grants_creados}")
