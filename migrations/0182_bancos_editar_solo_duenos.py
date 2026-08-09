"""Re-encadenar saldos y Saldos de apertura: sólo Accionista y Administrador.

TMT 2026-08-09 (dueña, mirando /usuarios/accesos): *"reencadenar solo
accionistas"*. Preguntado qué pasaba con `/bancos/apertura`, que cuelga del
MISMO permiso, eligió cerrarla también.

`bancos.editar` lo tenía un solo rol además de los wildcard: **INT** (Alex,
Irene, Maribel). Se lo habíamos dado el 2026-07-01 para re-encadenar la cadena
de saldos de un banco, que en su momento era mantenimiento y nada más.

⚠ Lo que cambió en el medio: el 2026-08-04 el saldo de un banco pasó a ser
`apertura + suma de los movimientos`, y la pantalla de apertura quedó detrás
de este mismo permiso. Desde entonces `bancos.editar` no es mantenimiento —
es el único número guardado del que sale el saldo del banco, y por lo tanto el
patrimonio y la utilidad. Corregir la apertura de Pichincha en $10.000 mueve
la utilidad $10.000 sin que haya entrado ni salido un peso. Un permiso puede
cambiar de peso sin que nadie lo mueva de lugar; éste lo hizo.

De acá en más las dos pantallas las tocan ella o Andrés. Los botones de
`/bancos` van detrás de `tiene_permiso('bancos.editar')`, así que a INT le
desaparecen en vez de darle 404 en la cara.

⚠ `config/roles.py` es la fuente canónica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migración el cambio existe sólo en el repo y
producción queda idéntica, con los tests en verde (lección migs 0164/0165).

Idempotente.
"""

from __future__ import annotations

import os

PERMISO = "bancos.editar"


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
    antes = [f[0] for f in cur.fetchall()]

    cur.execute(
        "DELETE FROM seguridad.permiso WHERE nombre_opcion = %s", (PERMISO,)
    )
    borrados = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        for rol in antes:
            print(f"    − {PERMISO} de {rol}")
        print(f"    {borrados} permiso(s) borrado(s); quedan sólo los wildcard")
