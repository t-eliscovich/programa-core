"""Permiso nuevo `retenciones.cargar_en_factura` — el lapicito de /facturas.

TMT 2026-08-19 (dueña, al ver que Alex no lo tenía): *"pero siempre tuvieron
acceso, ¿por qué no se los das?"*. Se lo damos a **INT** (Alex, Irene,
Maribel), **Cobranzas** y **Contabilidad**.

⭐ Por qué un permiso NUEVO y no devolverles `retenciones.emitir`: son dos
cosas distintas que hasta hoy compartían nombre.

  · `retenciones.emitir` abre el MÓDULO Retenciones: el listado y la pantalla
    `/retenciones/emitir`, que registra el comprobante pero **no baja el saldo
    de la factura** (queda pendiente de arreglar). La migración 0166 se lo
    sacó a INT el 05/08 con todo el paquete de "quitá activos, compras,
    gastos, iniciales, provisiones y retenciones de INT".
  · `retenciones.cargar_en_factura` es sólo el lapicito de la columna
    Retención en `/facturas`: la retención de la factura que estás mirando,
    aplicada de verdad (baja el saldo, recalcula el estado, deja el ↺).

Separarlos le devuelve a Alex lo que necesita para trabajar sin reabrirle lo
que la dueña cerró en agosto, y sin mandarla a la pantalla rota.

⚠ `config/roles.py` es la fuente canónica pero el que manda en runtime es
`seguridad.permiso`. Sin esta migración el permiso existe sólo en el repo y
producción queda idéntica, con los tests en verde (lección migs 0164/0165,
repetida en la 0182).

Idempotente: no inserta si el rol ya lo tiene.
"""

from __future__ import annotations

import os

PERMISO = "retenciones.cargar_en_factura"
ROLES_QUE_LO_RECIBEN = ("INT", "Cobranzas", "Contabilidad")


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
            # prueba o el rol se renombró. Mejor dejar constancia que inventar.
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
            print(f"    = {rol} ya lo tenía")
        for rol in sin_rol:
            print(f"    ! rol {rol} no existe en esta base — salteado")
