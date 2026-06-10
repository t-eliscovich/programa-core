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
import time as _time

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.asinfo")


# ---------------------------------------------------------------------------
# Helper genérico
# ---------------------------------------------------------------------------


def fetch_card_from_env(env_var: str, params: list[dict] | None = None) -> list[dict]:
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


def ventas_vendedor_usd(vendedor: str | None = None) -> list[dict]:
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


def ventas_vendedor_kg(vendedor: str | None = None) -> list[dict]:
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


def ventas_cliente_kg(vendedor: str | None = None) -> list[dict]:
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


def stock_asinfo(min_saldo: float = 0.0, id_bodega: int | None = None) -> list[dict]:
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
    try:
        id_bodega = int(id_bodega) if id_bodega is not None else None
    except (TypeError, ValueError):
        id_bodega = None
    cache_key = f"min_{min_saldo}_b{id_bodega}"
    now = _time.time()
    cached = _STOCK_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _STOCK_TTL_SECS:
        return cached[1]

    # Filtro opcional por bodega (para las tabs Hilo / Tela Cruda / Tela
    # Terminada de la vista "Por producto"). Sin bodega = consolida todas.
    filtro_bodega = f"WHERE id_bodega = {id_bodega}" if id_bodega is not None else ""

    # CRITICAL 2026-05-22: la vista `v_saldo_producto_vista` agrega LOTES
    # adicionales que el reporte oficial del ERP filtra (no respeta
    # "indicador lote"). Usando esa vista, los kg vienen ~6% más altos
    # que el reporte oficial. La fuente confiable es la tabla raw
    # `saldo_producto`, tomando el último snapshot por (producto, bodega)
    # con ROW_NUMBER. Verificado: Bodega Hilo = 1.767.920,41 kg coincide
    # al centavo con el export Excel del ERP.
    sql = f"""
        WITH ult AS (
            SELECT id_producto, id_bodega, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega
                       ORDER BY fecha DESC, id_saldo_producto DESC
                   ) AS rn
              FROM saldo_producto
              {filtro_bodega}
        )
        SELECT p.codigo                                                AS codigo,
               COALESCE(NULLIF(p.descripcion, ''), p.nombre, p.codigo) AS nombre,
               ''                                                      AS nombre_comercial,
               COALESCE(cp.nombre, '')                                 AS tejido,
               ''                                                      AS subcategoria,
               ''                                                      AS color,
               SUM(u.saldo)                                            AS cantidad_total,
               COUNT(DISTINCT u.id_bodega)                             AS n_bodegas,
               COALESCE(MAX(p.precio_ultima_venta), 0)                 AS precio_ultima
          FROM ult u
          INNER JOIN producto p ON p.id_producto = u.id_producto
          LEFT JOIN categoria_producto cp ON cp.id_categoria_producto = p.id_categoria_producto
         WHERE u.rn = 1 AND u.saldo > 0
         GROUP BY p.codigo, p.descripcion, p.nombre, cp.nombre
        HAVING SUM(u.saldo) > 0
         ORDER BY SUM(u.saldo) DESC
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


# ---------------------------------------------------------------------------
# Stock por LOTE (Asinfo) — réplica del reporte "Stock Valorado por Lote"
# ---------------------------------------------------------------------------
# A diferencia de stock_asinfo() (que consolida por producto sobre todas las
# bodegas), esta función baja al nivel de LOTE individual con sus atributos,
# que es lo que pide el reporte oficial del ERP. Fuente: `saldo_producto_lote`
# (snapshot diario por producto+bodega+lote) tomando el último snapshot por
# (producto, bodega, lote) con ROW_NUMBER. Verificado 2026-06-09 contra el
# reporte oficial:
#     Bodega Hilo        = 1.790.694,44 kg
#     Bodega Tela Cruda  =   255.795,25 kg   (reporte: 255.660 → ±0,05%)
#     Bodega Prod.Term.  =   347.389,93 kg
#
# Atributos del lote (tabla `lote`, slots EAV id_atributo_N/id_valor_atributo_N
# resueltos contra `valor_atributo`). Mapa de `atributo` (id → nombre):
#     1 = Acabado | 2 = Calidad (PRI/SEG) | 3 = Color | 51 = Estampado
#     101 = Titulo Hilo | 103 = Proveedor | 151 = Fallas PT | 152 = Fallas TC
# Bodegas: 1=Colorantes, 51=Hilo, 52=Tela Cruda, 53=Prod.Terminado,
#          151=Reproceso, 201=Cuarentena.
#
# Dólares: NO se traen de Asinfo (no confiables). Solo cantidad (kg).

_STOCK_LOTE_TTL_SECS = 600  # 10 minutos
_STOCK_LOTE_CACHE: dict = {}
_STOCK_LOTE_TOTALES_CACHE: dict = {}


def stock_asinfo_lote(
    id_bodega: int,
    q: str = "",
    tejido: str = "",
    titulo: str = "",
    proveedor: str = "",
    calidad: str = "",
    color: str = "",
    limit: int = 1500,
) -> list[dict]:
    """Stock por LOTE de una bodega, con atributos resueltos.

    TODOS los filtros se empujan al SQL. Cada fila incluye `_total_lotes` y
    `_total_kg` = COUNT/SUM OVER() del set filtrado COMPLETO (no del recorte),
    para que los KPIs sean correctos aunque la tabla muestre solo `limit` filas
    (Bodega Hilo sola tiene ~61k lotes; no se renderizan todas).

    Returns:
        Lista de dicts (≤ limit) ordenada por saldo DESC. [] si falla.
    """
    try:
        id_bodega = int(id_bodega)
    except (TypeError, ValueError):
        return []

    import time as _time

    def _esc(s: str) -> str:
        return (s or "").strip().replace("'", "''")

    q_norm = (q or "").strip().upper()
    cache_key = (
        f"b{id_bodega}_q{q_norm}_te{tejido}_ti{titulo}"
        f"_pr{proveedor}_ca{calidad}_co{color}_l{limit}"
    )
    now = _time.time()
    cached = _STOCK_LOTE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _STOCK_LOTE_TTL_SECS:
        return cached[1]

    # Filtros empujados al SQL (id_bodega ya es int sanitizado).
    filtro_q = ""
    if q_norm:
        s = _esc(q_norm)
        filtro_q += f" AND (UPPER(p.codigo) LIKE '%{s}%' OR UPPER(p.nombre) LIKE '%{s}%')"
    if tejido:
        filtro_q += f" AND cp.nombre = '{_esc(tejido)}'"
    if titulo:
        filtro_q += f" AND a.titulo_hilo = '{_esc(titulo)}'"
    if proveedor:
        filtro_q += f" AND a.proveedor = '{_esc(proveedor)}'"
    if calidad:
        filtro_q += f" AND UPPER(a.calidad) = '{_esc(calidad).upper()}'"
    if color:
        filtro_q += f" AND a.color = '{_esc(color)}'"

    sql = f"""
        WITH ult AS (
            SELECT id_producto, id_bodega, id_lote, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega, id_lote
                       ORDER BY fecha DESC, id_saldo_producto_lote DESC
                   ) AS rn
              FROM saldo_producto_lote
             WHERE id_bodega = {id_bodega}
        ),
        attr AS (
            SELECT u.id_lote,
                MAX(CASE WHEN va.id_atributo = 2   THEN va.nombre END) AS calidad,
                MAX(CASE WHEN va.id_atributo = 3   THEN va.nombre END) AS color,
                MAX(CASE WHEN va.id_atributo = 1   THEN va.nombre END) AS acabado,
                MAX(CASE WHEN va.id_atributo = 51  THEN va.nombre END) AS estampado,
                MAX(CASE WHEN va.id_atributo = 101 THEN va.nombre END) AS titulo_hilo,
                MAX(CASE WHEN va.id_atributo = 103 THEN va.nombre END) AS proveedor
              FROM lote l
              UNPIVOT (idv FOR slot IN (
                  id_valor_atributo_1, id_valor_atributo_2, id_valor_atributo_3,
                  id_valor_atributo_4, id_valor_atributo_5, id_valor_atributo_6,
                  id_valor_atributo_7, id_valor_atributo_8, id_valor_atributo_9,
                  id_valor_atributo_10)) u
              JOIN valor_atributo va ON va.id_valor_atributo = u.idv
             GROUP BY u.id_lote
        )
        SELECT TOP {int(limit)}
               p.codigo                                                AS codigo,
               COALESCE(NULLIF(p.descripcion, ''), p.nombre, p.codigo) AS producto,
               l.codigo                                                AS lote,
               COALESCE(cp.nombre, '')                                 AS tejido,
               a.calidad, a.color, a.acabado, a.estampado,
               a.titulo_hilo, a.proveedor,
               COALESCE(NULLIF(u.codigo, ''), u.nombre, 'KG')          AS unidad,
               ult.saldo                                               AS saldo,
               COUNT(*)        OVER ()                                  AS _total_lotes,
               SUM(ult.saldo)  OVER ()                                  AS _total_kg
          FROM ult
          INNER JOIN producto p ON p.id_producto = ult.id_producto
          INNER JOIN lote l ON l.id_lote = ult.id_lote
          LEFT JOIN categoria_producto cp ON cp.id_categoria_producto = p.id_categoria_producto
          LEFT JOIN unidad u ON u.id_unidad = p.id_unidad
          LEFT JOIN attr a ON a.id_lote = ult.id_lote
         WHERE ult.rn = 1 AND ult.saldo > 0 {filtro_q}
         ORDER BY ult.saldo DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=int(limit))
    out = []
    for r in rows:
        try:
            saldo = float(r.get("saldo") or 0)
            if saldo <= 0:
                continue
            out.append({
                "codigo": str(r.get("codigo") or "").strip(),
                "producto": str(r.get("producto") or "").strip(),
                "lote": str(r.get("lote") or "").strip(),
                "tejido": str(r.get("tejido") or "").strip(),
                "calidad": str(r.get("calidad") or "").strip(),
                "color": str(r.get("color") or "").strip(),
                "acabado": str(r.get("acabado") or "").strip(),
                "estampado": str(r.get("estampado") or "").strip(),
                "titulo_hilo": str(r.get("titulo_hilo") or "").strip(),
                "proveedor": str(r.get("proveedor") or "").strip(),
                "unidad": str(r.get("unidad") or "KG").strip() or "KG",
                "saldo": saldo,
                "_total_lotes": int(r.get("_total_lotes") or 0),
                "_total_kg": float(r.get("_total_kg") or 0),
            })
        except (TypeError, ValueError):
            continue
    _STOCK_LOTE_CACHE[cache_key] = (now, out)
    return out


def stock_asinfo_lote_totales() -> list[dict]:
    """Totales de stock por bodega a nivel lote (landing del reporte).

    Barato (un GROUP BY) — sirve de resumen y de ancla de reconciliación.
    Cada fila: id_bodega, bodega, lotes, total_kg.
    """
    import time as _time
    now = _time.time()
    cached = _STOCK_LOTE_TOTALES_CACHE.get("all")
    if cached and (now - cached[0]) < _STOCK_LOTE_TTL_SECS:
        return cached[1]

    sql = """
        WITH ult AS (
            SELECT id_producto, id_bodega, id_lote, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega, id_lote
                       ORDER BY fecha DESC, id_saldo_producto_lote DESC
                   ) AS rn
              FROM saldo_producto_lote
        )
        SELECT b.id_bodega AS id_bodega,
               b.nombre    AS bodega,
               COUNT(*)    AS lotes,
               SUM(u.saldo) AS total_kg
          FROM ult u
          JOIN bodega b ON b.id_bodega = u.id_bodega
         WHERE u.rn = 1 AND u.saldo > 0
         GROUP BY b.id_bodega, b.nombre
         ORDER BY SUM(u.saldo) DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=100)
    out = []
    for r in rows:
        try:
            out.append({
                "id_bodega": int(r.get("id_bodega")),
                "bodega": str(r.get("bodega") or "").strip(),
                "lotes": int(r.get("lotes") or 0),
                "total_kg": float(r.get("total_kg") or 0),
            })
        except (TypeError, ValueError):
            continue
    _STOCK_LOTE_TOTALES_CACHE["all"] = (now, out)
    return out


# ---------------------------------------------------------------------------
# Stock EN PROCESO (WIP entre pasos de producción)
# ---------------------------------------------------------------------------
# Material despachado a órdenes de fabricación ABIERTAS pero todavía no
# devuelto como el producto del siguiente paso → "stock entre pasos" que no
# está en ningún saldo de bodega. Definición live (verificada 2026-06-09):
#   issued = SUM(detalle_orden_salida_material.cantidad_despachada) por OFT,
#            vía la junction detalle_orden_salida_material_orden_fabricacion.
#            (OFT-000035309 → 4.950,72, coincide al centavo con el Excel.)
#   producido = orden_fabricacion.cantidad_fabricada (declarado, estado actual).
#   en_proceso = issued − producido.
# Pasos por bodega de salida de la OFT: 52 = Tejeduría (Hilo→Tela Cruda),
# 53 = Tintorería/Confección (Tela Cruda→Producto Terminado).

_EN_PROCESO_TTL_SECS = 600
_EN_PROCESO_CACHE: dict = {}

_PASOS_PROCESO = {52: "Tejeduría (Hilo → Tela Cruda)", 53: "Tintorería (Tela Cruda → PT)"}


def stock_en_proceso() -> dict:
    """WIP entre pasos. Devuelve {pasos: [...], ofts: [...]} (fail-soft).

    pasos: por bodega de salida — issued, producido, en_proceso, n_ofts.
    ofts:  detalle por OFT con en_proceso > 0 (material claramente en proceso).
    """
    import time as _time
    now = _time.time()
    cached = _EN_PROCESO_CACHE.get("all")
    if cached and (now - cached[0]) < _EN_PROCESO_TTL_SECS:
        return cached[1]

    sql = """
        WITH ofs AS (
            SELECT id_orden_fabricacion, numero, id_bodega, id_producto,
                   ISNULL(cantidad, 0) AS planif, ISNULL(cantidad_fabricada, 0) AS fab
              FROM orden_fabricacion
             WHERE id_bodega IN (52, 53) AND estado_produccion <> 5
        ),
        issued AS (
            SELECT j.id_orden_fabricacion,
                   SUM(ISNULL(d.cantidad_despachada, 0)) AS issued
              FROM detalle_orden_salida_material_orden_fabricacion j
              JOIN detalle_orden_salida_material d
                ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
             WHERE j.id_orden_fabricacion IN (SELECT id_orden_fabricacion FROM ofs)
             GROUP BY j.id_orden_fabricacion
        )
        SELECT o.id_bodega                                      AS id_bodega,
               o.numero                                         AS oft,
               COALESCE(NULLIF(p.descripcion,''), p.nombre, p.codigo) AS producto,
               p.codigo                                         AS prod_codigo,
               o.planif                                         AS planif,
               o.fab                                            AS fab,
               ISNULL(i.issued, 0)                              AS issued,
               ISNULL(i.issued, 0) - o.fab                      AS en_proceso
          FROM ofs o
          LEFT JOIN issued i ON i.id_orden_fabricacion = o.id_orden_fabricacion
          LEFT JOIN producto p ON p.id_producto = o.id_producto
         ORDER BY ISNULL(i.issued, 0) - o.fab DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=20000)
    pasos: dict[int, dict] = {}
    ofts = []
    for r in rows:
        try:
            b = int(r.get("id_bodega"))
            issued = float(r.get("issued") or 0)
            fab = float(r.get("fab") or 0)
            ep = float(r.get("en_proceso") or 0)
            slot = pasos.setdefault(b, {"id_bodega": b, "paso": _PASOS_PROCESO.get(b, f"Bodega {b}"),
                                        "issued": 0.0, "producido": 0.0, "en_proceso": 0.0, "n_ofts": 0})
            slot["issued"] += issued
            slot["producido"] += fab
            slot["en_proceso"] += ep
            slot["n_ofts"] += 1
            if ep > 0.01:
                ofts.append({
                    "id_bodega": b,
                    "paso": _PASOS_PROCESO.get(b, f"Bodega {b}"),
                    "oft": str(r.get("oft") or "").strip(),
                    "producto": str(r.get("producto") or "").strip(),
                    "prod_codigo": str(r.get("prod_codigo") or "").strip(),
                    "planif": float(r.get("planif") or 0),
                    "fab": fab,
                    "issued": issued,
                    "en_proceso": ep,
                })
        except (TypeError, ValueError):
            continue
    out = {"pasos": sorted(pasos.values(), key=lambda x: x["id_bodega"]), "ofts": ofts}
    _EN_PROCESO_CACHE["all"] = (now, out)
    return out


# ---------------------------------------------------------------------------
# Importaciones de Asinfo (para cruzar contra compras/anticipos del programa)
# ---------------------------------------------------------------------------
# La lista "Importación" del ERP. La `Nota` (factura_proveedor.descripcion)
# lleva el código de la compra/anticipo del programa al final — ver
# concepto_parser.parse_nota_importacion(). Los DÓLARES de Asinfo (total) son
# referenciales; los confiables vienen del programa (scintela.compra).

_IMPORT_TTL_SECS = 300  # 5 minutos
_IMPORT_CACHE: dict = {}


def importaciones_asinfo(limite: int = 400) -> list[dict]:
    """Lista de importaciones del ERP, con su Nota (código de cruce).

    Cada fila:
        im_numero       — número de importación user-facing (IM-0000537)
        fecha           — fecha de la factura proveedor (YYYY-MM-DD)
        fecha_recepcion — fecha de recepción (YYYY-MM-DD o None)
        total_asinfo    — total del ERP (REFERENCIAL — no confiable)
        proveedor       — razón comercial/fiscal de la empresa
        prov_cod_asinfo — código de empresa en Asinfo (≠ código del programa)
        nota            — descripción libre; lleva el código del programa

    Returns:
        Lista de dicts ordenada por importación más reciente primero.
        [] si Metabase no está configurado o falla (fail-soft).
    """
    import time as _time
    cache_key = f"l{int(limite)}"
    now = _time.time()
    cached = _IMPORT_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _IMPORT_TTL_SECS:
        return cached[1]

    # La "Fecha Recepción" real es la del documento de recepción de mercadería
    # (recepcion_proveedor.fecha, doc BOD-…), NO fp.fecha_recepcion (siempre NULL).
    # Si la importación todavía no tiene recepción → en tránsito (no recibida).
    sql = f"""
        SELECT TOP {int(limite)}
               fp.numero                                       AS im_numero,
               CONVERT(varchar, fp.fecha, 23)                  AS fecha,
               CONVERT(varchar, rp.fecha, 23)                  AS fecha_recepcion,
               rp.numero                                       AS bod,
               fp.total                                        AS total_asinfo,
               COALESCE(e.nombre_comercial, e.nombre_fiscal, '') AS proveedor,
               e.codigo                                        AS prov_cod_asinfo,
               fp.descripcion                                  AS nota
          FROM factura_proveedor_importacion fpi
          JOIN factura_proveedor fp ON fp.id_factura_proveedor = fpi.id_factura_proveedor
          LEFT JOIN empresa e ON e.id_empresa = fp.id_empresa
          LEFT JOIN recepcion_proveedor rp ON rp.id_recepcion_proveedor = fp.id_recepcion_proveedor
         ORDER BY fpi.id_factura_proveedor DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=int(limite))
    out = []
    for r in rows:
        try:
            frec = str(r.get("fecha_recepcion") or "").strip() or None
            out.append({
                "im_numero": str(r.get("im_numero") or "").strip(),
                "fecha": str(r.get("fecha") or "").strip() or None,
                "fecha_recepcion": frec,
                "recibida": frec is not None,
                "bod": str(r.get("bod") or "").strip(),
                "total_asinfo": float(r.get("total_asinfo") or 0),
                "proveedor": str(r.get("proveedor") or "").strip(),
                "prov_cod_asinfo": str(r.get("prov_cod_asinfo") or "").strip(),
                "nota": str(r.get("nota") or "").strip(),
            })
        except (TypeError, ValueError):
            continue
    _IMPORT_CACHE[cache_key] = (now, out)
    return out
