"""Lookups rápidos para autocomplete — devuelven (codigo, nombre) de todos
los clientes/proveedores activos. Se cargan en memoria por render y se
exponen al template como `clientes_datalist` / `proveedores_datalist`.

Si la lista supera 2000 items el dataset empieza a pesar; hasta entonces
esto es más simple que un autocomplete async.
"""
from __future__ import annotations

import db


def clientes_para_datalist() -> list[dict]:
    """Todos los clientes (activos + inactivos), ordenados alfabético por código. Max 5000.

    TMT 2026-05-26 dueña #1: BED (HECTOR BEDON) está marcado INACTIVO pero
    tiene $361K de cartera viva — necesitamos poder cobrarle. Antes el
    filtro `activo <> '0'` lo escondía del autocomplete de /cheques/nuevo.

    TMT 2026-05-26 dueña #2: el sort "activos primero" hundía BED al final
    de la lista al tipear "BED" — debajo de EVB/FBE/LCB/LOC/SAB/SYB/TOB/VPB
    (otros clientes con "BED" en el nombre). El browser filtra el datalist
    por el texto tipeado pero muestra los matches en orden HTML, así que
    BED quedaba abajo de todo. Fix: solo `ORDER BY codigo_cli` — alfabético
    puro. BED es lo primero al tipear "BED" porque alfabéticamente es lo
    primero.
    """
    return db.fetch_all(
        """
        SELECT codigo_cli, COALESCE(nombre, '') AS nombre
          FROM scintela.cliente
         ORDER BY codigo_cli
         LIMIT 5000
        """
    ) or []


def proveedores_para_datalist() -> list[dict]:
    return db.fetch_all(
        """
        SELECT codigo_prov, COALESCE(nombre, '') AS nombre
          FROM scintela.proveedor
         WHERE COALESCE(activo, '1') <> '0'
         ORDER BY codigo_prov
         LIMIT 5000
        """
    ) or []
