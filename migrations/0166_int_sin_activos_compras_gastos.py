"""INT pierde activos, compras, gastos, iniciales, provisiones y retenciones.

TMT 2026-08-05 (dueña): *"quitá activos y quitá compras, quitá gastos, quitá
iniciales, quitá provisiones, quitá retenciones de INT"*.

Son **18 permisos** de seis módulos, y NO es sólo lectura: se va también la
operativa (`compras.crear/editar/anular`, `gastos.crear/editar/anular`,
`activos.crear/amortizar`, `provisiones.crear/editar`,
`retenciones.emitir/anular`, `iniciales.editar`). Alex, Irene y Maribel dejan
de cargar compras, gastos, activos, provisiones y retenciones.

Al rol le quedan 34 permisos: cobranza, cheques, facturas, bancos, caja,
clientes, proveedores, tintorería, ventas-año y dólares.

⚠ Se borran SÓLO del rol INT. `gastos.anular` y `gastos.editar` no los tiene
ningún otro rol, así que a partir de acá esos dos existen únicamente para los
wildcard (Accionista y Administrador) — es la consecuencia buscada, pero
conviene tenerlo escrito.

⚠ Como la 0164 y la 0165: `config/roles.py` es la fuente canónica pero el que
manda en runtime es `seguridad.permiso`. Sin esta migración el cambio existe
sólo en el repo.

Idempotente.
"""

from __future__ import annotations

import os

PREFIJOS = ("activos.", "compras.", "gastos.", "iniciales.",
            "provisiones.", "retenciones.")
ROL = "INT"


def run(conn) -> None:
    cur = conn.cursor()

    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = %s", (ROL,))
    fila = cur.fetchone()
    if not fila:                       # pragma: no cover — el rol existe desde la 0043
        cur.close()
        return
    id_rol = fila[0]

    patrones = [p + "%" for p in PREFIJOS]
    cur.execute(
        """
        SELECT nombre_opcion
          FROM seguridad.permiso
         WHERE id_rol = %s AND nombre_opcion LIKE ANY(%s)
         ORDER BY 1
        """,
        (id_rol, patrones),
    )
    antes = [r[0] for r in cur.fetchall()]

    cur.execute(
        "DELETE FROM seguridad.permiso WHERE id_rol = %s AND nombre_opcion LIKE ANY(%s)",
        (id_rol, patrones),
    )
    borrados = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        for p in antes:
            print(f"    − {p}")
        print(f"    {borrados} permiso(s) borrado(s) del rol {ROL}")
