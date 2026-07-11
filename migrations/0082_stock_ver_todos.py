"""Conceder `stock.ver` a TODOS los roles.

Pedido dueña 2026-06-09: "Stock lo ve todo el mundo". La nueva sección
Stock del sidebar (Resumen / Por lote Asinfo / Por producto Asinfo /
Importaciones) y sus vistas pasaron de `informes.ver` a `stock.ver`
(ver config/roles.py). Para que los roles NO-wildcard la vean, hay que
sembrar el permiso en seguridad.permiso.

Roles afectados (todos los que existen, menos los wildcard):
    - Gerente, Contabilidad, Compras, Cobranzas, Ventas, QC, Alex, Lectura → +stock.ver
    - Bodega ya tenía stock.ver (incluido igual — idempotente).
    - Accionista / Administrador usan `*` (no necesitan grant explícito).

Schema (per 0003_seed_roles.py):
    seguridad.permiso(id_permiso, id_rol, nombre_opcion)
    UNIQUE (id_rol, nombre_opcion)

Idempotente: ON CONFLICT DO NOTHING en (id_rol, nombre_opcion).
"""
from __future__ import annotations

ROLES = (
    "Gerente",
    "Contabilidad",
    "Compras",
    "Cobranzas",
    "Ventas",
    "QC",
    "Alex",
    "Lectura",
    "Bodega",
)


def run(conn) -> None:
    cur = conn.cursor()

    grants_creados = 0
    for nombre_rol in ROLES:
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
            (id_rol, "stock.ver"),
        )
        if cur.rowcount:
            grants_creados += 1
            print(f"  + {nombre_rol} -> stock.ver")
        else:
            print(f"  = {nombre_rol} ya tenia stock.ver")

    print(f"  Total grants nuevos: {grants_creados}")
