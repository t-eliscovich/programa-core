"""Lookups rápidos para autocomplete — devuelven (codigo, nombre) de todos
los clientes/proveedores activos. Se cargan en memoria por render y se
exponen al template como `clientes_datalist` / `proveedores_datalist`.

Si la lista supera 2000 items el dataset empieza a pesar; hasta entonces
esto es más simple que un autocomplete async.
"""
from __future__ import annotations

import db


def clientes_para_datalist() -> list[dict]:
    """Todos los clientes activos, ordenados por código. Max 2000."""
    return db.fetch_all(
        """
        SELECT codigo_cli, COALESCE(nombre, '') AS nombre
          FROM scintela.cliente
         WHERE COALESCE(activo, '1') <> '0'
         ORDER BY codigo_cli
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
