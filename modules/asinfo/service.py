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


# ---------------------------------------------------------------------------
# Stock — cantidad por producto (sin costo: Asinfo no tiene costos cargados)
# ---------------------------------------------------------------------------
# Confirmado 2026-05-22: ninguno de los 20.064 productos activos tiene
# `costo_estandar` ni `precio_referencial_compra` > 0. Solo `precio_ultima_venta`
# tiene data para ~24% de los productos. Por eso `stock_asinfo()` devuelve
# CANTIDAD de stock (saldo_producto.saldo) sin costo. Si en algún momento
# se cargan costos en el ERP, esta función puede ampliarse para incluirlos.

_STOCK_TTL_SECS = 600  # 10 minutos
_STOCK_CACHE: dict = {}


def stock_asinfo(min_saldo: float = 0.0) -> list[dict]:
    """Stock por producto desde Asinfo, usando la vista pre-calculada
    `v_saldo_producto_vista` que ya:
      - consolida el saldo más reciente por producto+bodega,
      - expone tejido (categoría), subcategoría, color (hex),
      - incluye nombre_producto y nombre_comercial.

    Cada fila:
        codigo            — código SKU del producto
        nombre            — nombre_producto (p.ej. "Jersey 3.5 BLA")
        nombre_comercial  — alternativo (puede estar vacío)
        tejido            — nombre_categoria_producto (Jersey / Fleece / Pique / Rib / etc.)
        subcategoria      — variante (3.5 / 1.2x2.3 / 24/1 / etc.)
        color             — hex Asinfo (#ffffff). El "nombre" del color va embebido
                            en codigo (BLA, NEG, MAR, ...) y nombre.
        cantidad_total    — SUM(saldo_acumulado) sobre todas las bodegas del producto
        n_bodegas         — cuántas bodegas distintas tienen stock
        precio_ultima     — precio_ultima_venta del producto (puede ser 0)

    Args:
        min_saldo: filtra a productos con cantidad_total > min_saldo. Default 0.

    Returns:
        Lista de dicts ordenada por cantidad_total DESC. [] si Metabase no
        está configurado o si falla.

    Nota perf 2026-05-22: la vista trae ~3500 productos con stock. Subimos
    el max-results de Metabase a 10000 para no truncar (default 2000).
    """
    import time as _time
    cache_key = f"min_{min_saldo}"
    now = _time.time()
    cached = _STOCK_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _STOCK_TTL_SECS:
        return cached[1]

    sql = """
        SELECT codigo_producto                       AS codigo,
               nombre_producto                       AS nombre,
               COALESCE(nombre_comercial, '')        AS nombre_comercial,
               COALESCE(nombre_categoria_producto, '') AS tejido,
               COALESCE(nombre_subcategoria_producto, '') AS subcategoria,
               COALESCE(color, '')                   AS color,
               SUM(saldo)                            AS cantidad_total,
               COUNT(DISTINCT id_bodega)             AS n_bodegas,
               MAX(COALESCE(precio_ultima_venta, 0)) AS precio_ultima
          FROM v_saldo_producto_vista
         WHERE saldo > 0
         GROUP BY codigo_producto, nombre_producto, nombre_comercial,
                  nombre_categoria_producto, nombre_subcategoria_producto, color
        HAVING SUM(saldo) > 0
         ORDER BY SUM(saldo) DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=10000)
    if not rows:
        return []
    out = []
    for r in rows:
        try:
            qty = float(r.get("cantidad_total") or 0)
            if qty < min_saldo:
                continue
            nombre = str(r.get("nombre") or "").strip()
            codigo = str(r.get("codigo") or "").strip()
            # Extraer "código de color" desde el nombre o código.
            # El convenio en Asinfo es que el color va al final, separado por
            # espacio o guion (ej. "Jersey 3.5 BLA", "20/1-65:35-PEI-KW").
            # Tomamos el último token alfa-mayúsculas del nombre; fallback al
            # último token del código si el nombre no tiene uno claro.
            color_cod = ""
            for token in reversed(nombre.split()):
                t = token.strip().upper()
                if t.isalpha() and 2 <= len(t) <= 5:
                    color_cod = t
                    break
            if not color_cod:
                # Fallback: último token del código separado por '-'
                last = codigo.split("-")[-1].strip().upper()
                if last.isalpha() and 2 <= len(last) <= 5:
                    color_cod = last
            out.append({
                "codigo": codigo,
                "nombre": nombre,
                "nombre_comercial": str(r.get("nombre_comercial") or "").strip(),
                "tejido": str(r.get("tejido") or "").strip(),
                "subcategoria": str(r.get("subcategoria") or "").strip(),
                "color_hex": str(r.get("color") or "").strip(),  # hex Asinfo (a menudo no es real)
                "color": color_cod,                              # el "código de color" útil (BLA/NEG/etc.)
                "cantidad_total": qty,
                "n_bodegas": int(r.get("n_bodegas") or 0),
                "precio_ultima": float(r.get("precio_ultima") or 0),
                # Compat con código viejo que esperaba estos nombres:
                "descripcion": nombre,
                "bodegas_detalle": "",
            })
        except (TypeError, ValueError):
            continue
    _STOCK_CACHE[cache_key] = (now, out)
    return out
