"""Lookups rápidos para autocomplete — devuelven (codigo, nombre) de todos
los clientes/proveedores activos. Se cargan en memoria por render y se
exponen al template como `clientes_datalist` / `proveedores_datalist`.

Si la lista supera 2000 items el dataset empieza a pesar; hasta entonces
esto es más simple que un autocomplete async.
"""
from __future__ import annotations

import db


def clientes_para_datalist() -> list[dict]:
    """Todos los clientes (activos + inactivos), ordenados por código. Max 2000.

    TMT 2026-05-26 dueña: BED (HECTOR BEDON) está marcado INACTIVO pero
    tiene $361K de cartera viva — necesitamos poder cobrarle. Antes el
    filtro `activo <> '0'` lo escondía del autocomplete de /cheques/nuevo.
    Ahora traemos TODOS (los inactivos quedan al final por sort).
    """
    return db.fetch_all(
        """
        SELECT codigo_cli, COALESCE(nombre, '') AS nombre
          FROM scintela.cliente
         ORDER BY
           CASE WHEN COALESCE(activo, '1') <> '0' THEN 0 ELSE 1 END,
           codigo_cli
         LIMIT 2000
        """
    ) or []


def proveedores_para_datalist() -> list[dict]:
    return db.fetch_all(
        """
        SELECT codigo_prov, COALESCE(nombre, '') AS nombre
          FROM scintela.proveedor
         WHERE COALESCE(activo, '1') <> '0'
         ORDER BY codigo_prov
         LIMIT 2000
        """
    ) or []
