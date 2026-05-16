"""Fachada estable del módulo costos_ot.

El resto del app importa desde acá, nunca directo de `adapters`. Así si cambia
el backend (fake → metabase → postgres) no hay que tocar views/templates.

El adapter se resuelve lazy en el primer uso para que tests puedan monkey-patch
`_get_adapter` sin levantar el módulo entero.
"""
from __future__ import annotations

import logging

from modules.costos_ot.adapters import CostosOTAdapter, OTCosto, build_adapter

_LOG = logging.getLogger("programa_core.costos_ot")

_adapter: CostosOTAdapter | None = None


def _get_adapter() -> CostosOTAdapter:
    global _adapter
    if _adapter is None:
        _adapter = build_adapter()
        _LOG.info("costos_ot adapter = %s", getattr(_adapter, "fuente", type(_adapter).__name__))
    return _adapter


def reset_adapter(adapter: CostosOTAdapter | None = None) -> None:
    """Para tests: resetea el singleton. Si se pasa adapter, lo fija."""
    global _adapter
    _adapter = adapter


def costos_por_cliente(codigo_cli: str) -> list[OTCosto]:
    try:
        return list(_get_adapter().costos_por_cliente(codigo_cli))
    except Exception as e:
        _LOG.warning("costos_por_cliente(%r) falló: %s", codigo_cli, e)
        return []


def costos_por_factura(id_factura: int) -> list[OTCosto]:
    try:
        return list(_get_adapter().costos_por_factura(id_factura))
    except Exception as e:
        _LOG.warning("costos_por_factura(%r) falló: %s", id_factura, e)
        return []


def disponible() -> bool:
    try:
        return bool(_get_adapter().disponible())
    except Exception:
        return False


def fuente() -> str:
    """Etiqueta corta de qué adapter está activo — se muestra en la UI."""
    a = _get_adapter()
    return getattr(a, "fuente", type(a).__name__).lower()
