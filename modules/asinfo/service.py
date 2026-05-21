"""Bridge a Asinfo (ERP SQL Server) vía Metabase API.

Asinfo no es alcanzable directo desde Programa Core (firewall + dialecto
SQL Server). Pero Metabase corre en el mismo EC2 que Programa Core, ya
tiene la conexión configurada como Database 2, y tiene cards SQL escritas,
debugueadas y auditadas para los reportes principales del ERP.

Este módulo expone esas cards como funciones planas. Cada función:

    - Resuelve el card_id desde una env var con prefijo ASINFO_CARD_*.
      Eso permite rotar cards (corrigieron un bug → nueva versión) sin
      tocar código.
    - Llama metabase_client.fetch_card(card_id), que devuelve list[dict].
    - Devuelve los rows tal cual — sin parsear a dataclass, porque cada
      card tiene su propio shape de columnas que ya está definido en
      Metabase. Si más adelante una vista necesita estructura tipada,
      se agrega un dataclass específico encima.

Las funciones son fail-soft (lo que devuelve metabase_client): si Metabase
está caído o las env vars no están seteadas, retornan [].

Env vars que lee:
    METABASE_URL, METABASE_USERNAME, METABASE_PASSWORD  (vía metabase_client)
    ASINFO_CARD_VENDEDOR_USD     — card_id de "Vendedor Comparativo Interanual USD"
    ASINFO_CARD_VENDEDOR_KG      — card_id de "Vendedor Kg"
    ASINFO_CARD_CLIENTE_KG       — card_id de "Cliente Kg"

Las cards canónicas al día de hoy (ver `intela-aws-deploy` SKILL):
    116 — Vendedor - Comparativo Interanual (USD)
    163 — Vendedor Kg - Comparativo Interanual
    164 — Cliente Kg - Comparativo Interanual
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.asinfo")


# ---------------------------------------------------------------------------
# Helper genérico
# ---------------------------------------------------------------------------


def fetch_card_from_env(env_var: str, params: Optional[list[dict]] = None) -> list[dict]:
    """Lee la card cuyo ID está en la env var y la ejecuta vía Metabase.

    Args:
        env_var: nombre de la env var que contiene el card_id (ej.
                 "ASINFO_CARD_VENDEDOR_USD").
        params: parameters opcionales para template-tags de la card,
                en el formato Metabase ({"type", "target", "value"}).

    Returns:
        Lista de rows (dicts). [] si la env var está vacía o Metabase falla.
    """
    card_id = os.environ.get(env_var, "").strip()
    if not card_id:
        _LOG.info("%s vacío — devolviendo []", env_var)
        return []
    return metabase_client.fetch_card(card_id, params=params)


# ---------------------------------------------------------------------------
# Wrappers nominales — las cards canónicas de ERP
# ---------------------------------------------------------------------------


def ventas_vendedor_usd(vendedor: Optional[str] = None) -> list[dict]:
    """Comparativo interanual de ventas en USD por vendedor.

    Si la card tiene un template-tag `{{vendedor}}` (opcional), se le pasa
    el filtro. Si no, devuelve todos los vendedores.
    """
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_VENDEDOR_USD", params=params)


def ventas_vendedor_kg(vendedor: Optional[str] = None) -> list[dict]:
    """Comparativo interanual de ventas en Kg por vendedor."""
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_VENDEDOR_KG", params=params)


def ventas_cliente_kg(vendedor: Optional[str] = None) -> list[dict]:
    """Comparativo interanual de ventas en Kg por cliente (con filtro
    opcional por vendedor — la card 164 tiene ese template-tag)."""
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_CLIENTE_KG", params=params)


# ---------------------------------------------------------------------------
# Cache TTL para facturas_periodo
# ---------------------------------------------------------------------------
# Una vista de /facturas pide hasta 5 años de facturas a Metabase y eso
# tarda 10-30s. Cacheamos por rango de fechas durante 5 min — la cartera
# no cambia segundo a segundo y la diferencia entre "ahora" y "hace 5 min"
# es irrelevante para conciliación. Reiniciar el proceso lo invalida.

import time as _time

_FACTURAS_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_FACTURAS_TTL_SECS = 300  # 5 minutos


def facturas_periodo(desde, hasta) -> list[dict]:
    """Facturas + NC financieras + Devoluciones + NTEN en el rango [desde, hasta].

    Cada fila es un documento individual con:
        tipo            — 'FACTURA' | 'DEVOLUCION' | 'NC_FINANCIERA' | 'NTEN' | 'NCNT'
        fecha           — date
        numero          — TEXT ('001-099-000175661' o 'NTEN-10444')
        cliente_codigo  — TEXT (código user-facing de empresa)
        vendedor        — TEXT (nombre del agente comercial)
        kg              — DECIMAL. NC_FINANCIERA siempre 0. DEVOLUCION/NCNT negados.
        usd             — DECIMAL. NC_FINANCIERA, DEVOLUCION y NCNT negados.

    Lee de la card definida por la env var `ASINFO_CARD_FACTURAS` (ID 199 al 2026-05-21).

    Performance: cache TTL 5 min por (desde, hasta). La primera carga del día
    sobre un rango grande puede tardar 10-30s; las siguientes son instantáneas.
    Para invalidar el cache, llamar `reset_facturas_cache()` o restart del proceso.

    Args:
        desde, hasta: `date` o string 'YYYY-MM-DD'. Ambos inclusivos.

    Returns:
        Lista de dicts con las columnas arriba. [] si Metabase está caído
        o si la env var no está seteada (fail-soft).
    """
    if hasattr(desde, "isoformat"):
        desde = desde.isoformat()
    if hasattr(hasta, "isoformat"):
        hasta = hasta.isoformat()
    desde, hasta = str(desde), str(hasta)

    key = (desde, hasta)
    now = _time.time()
    cached = _FACTURAS_CACHE.get(key)
    if cached and (now - cached[0]) < _FACTURAS_TTL_SECS:
        return cached[1]

    params = [
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_inicio"]],
            "value": desde,
        },
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_fin"]],
            "value": hasta,
        },
    ]
    rows = fetch_card_from_env("ASINFO_CARD_FACTURAS", params=params)
    # Solo cacheamos si trajo algo — si fue [] por error de red, no fijamos
    # el resultado vacío 5 min (mejor reintentar al próximo request).
    if rows:
        _FACTURAS_CACHE[key] = (now, rows)
    return rows


def reset_facturas_cache() -> None:
    """Vaciar el cache de facturas_periodo. Útil para tests o tras un deploy
    que invalidó la fuente."""
    _FACTURAS_CACHE.clear()


def facturas_totales_por_tipo(desde, hasta) -> dict:
    """Agregados por tipo de documento en el período [desde, hasta].

    Wrapper sobre facturas_periodo() que suma kg y usd por tipo. Útil para
    el panel de "ventas del mes" y para conciliar contra Programa Core.

    Returns:
        dict tipo→{docs, kg, usd}. Vacío si no hay data o si el bridge cae.
    """
    rows = facturas_periodo(desde, hasta)
    out: dict = {}
    for r in rows:
        t = r.get("tipo") or "?"
        slot = out.setdefault(t, {"docs": 0, "kg": 0.0, "usd": 0.0})
        slot["docs"] += 1
        slot["kg"] += float(r.get("kg") or 0)
        slot["usd"] += float(r.get("usd") or 0)
    # Redondear
    for slot in out.values():
        slot["kg"] = round(slot["kg"], 3)
        slot["usd"] = round(slot["usd"], 2)
    return out


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------


def disponible() -> bool:
    """True si Metabase está configurado y al menos una card_id está seteada.

    No prueba conectividad (eso es trabajo del healthcheck). Solo dice si
    el bridge está armado a nivel configuración.
    """
    if not metabase_client.disponible():
        return False
    return any(
        os.environ.get(v, "").strip()
        for v in (
            "ASINFO_CARD_VENDEDOR_USD",
            "ASINFO_CARD_VENDEDOR_KG",
            "ASINFO_CARD_CLIENTE_KG",
            "ASINFO_CARD_FACTURAS",
        )
    )
