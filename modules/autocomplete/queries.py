"""Lookups rápidos para autocomplete — devuelven (codigo, nombre) de todos
los clientes/proveedores activos. Se cargan en memoria por render y se
exponen al template como `clientes_datalist` / `proveedores_datalist`.

Si la lista supera 2000 items el dataset empieza a pesar; hasta entonces
esto es más simple que un autocomplete async.
"""
from __future__ import annotations

import db


def clientes_para_datalist() -> list[dict]:
    """Todos los clientes, ordenados alfabético por código. Max 5000.

    TMT 2026-05-26 dueña: el campo `activo` NO se usa como filtro en
    Intela (todos los clientes son tratables como activos). Versiones
    anteriores tenían `WHERE activo<>'0'` y un sort "activos primero" que
    escondía/hundía clientes — quedó eliminado. Sort puro alfabético por
    codigo_cli para que al tipear "BED" el match exacto aparezca primero
    (antes que EVB/FBE/LCB/LOC/SAB/SYB/TOB/VPB).
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
        SELECT codigo_prov, COALESCE(nombre, '') AS nombre,
               UPPER(TRIM(COALESCE(tipo, ''))) AS tipo
          FROM scintela.proveedor
         WHERE COALESCE(activo, '1') <> '0'
         ORDER BY codigo_prov
         LIMIT 5000
        """
    ) or []
