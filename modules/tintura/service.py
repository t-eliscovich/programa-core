"""Bridge a formulas_app — datos de tintorería para Programa Core.

Propósito acotado (decisión 2026-05-21): formulas_app NO tiene clientes;
es una app de tintorería pura. Lo que sí tiene y nos interesa traer:

    - Por orden: kg de tela cruda (entrante) → kg de tela terminada (salida)
      → desperdicio (= crudos − terminados).
    - Stock de productos químicos: última lectura de inventario por producto,
      con su precio catalog (productos.us).

Este módulo expone funciones planas (no clase, no adapter). Cada función
devuelve una lista de dataclasses inmutables, lista para que un consumidor
(otra vista, otro servicio, un endpoint JSON, lo que sea) la procese.

Nunca rompe el host: si formulas_db no está configurado o la query falla,
devuelve [] con un log a WARNING. La regla habitual del adapter pattern de
Programa Core.

Schema de origen verificado contra /Users/tamaraeliscovich/Documents/Claude/
Projects/Intela/formulas_app/database.py al 2026-05-21:

    ordenes(id, numero, fecha, codigo, kil, jet, rel, lit, created_at,
            tela_cruda_kg, tela_terminada_kg, fecha_terminado, es_reproceso,
            observaciones, bano_numero)
    formulas(cod, color, categoria, grupo)
    productos(num, num_visible, nombre, prov, us, unidad, familia)
    inventario(id, producto_num, cantidad, fecha, nota, created_at)

    ordenes.fecha           — TEXT 'DD/MM/YYYY'  (legacy)
    ordenes.fecha_terminado — TEXT 'YYYY-MM-DD'  (ISO)
    inventario.fecha        — TEXT 'YYYY-MM-DD'  (ISO; las queries de
                              formulas_app lo comparan lex con '<=')
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from modules._lib import formulas_db

_LOG = logging.getLogger("programa_core.tintura")


# ---------------------------------------------------------------------------
# Dataclasses de salida
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TinturadoOrden:
    """Resumen de una orden de tintorería: kg in/out/desperdicio.

    desperdicio_kg = tela_cruda_kg - tela_terminada_kg, solo si ambos
    están cargados. None si falta alguno.
    """

    numero: str
    fecha: Optional[date]                  # parsed de fecha DD/MM/YYYY → date
    fecha_terminado: Optional[date]        # parsed de fecha YYYY-MM-DD → date
    formula_cod: Optional[str]
    color: Optional[str]
    categoria: Optional[str]
    kilos_planeados: float                 # ordenes.kil
    tela_cruda_kg: Optional[float]
    tela_terminada_kg: Optional[float]
    desperdicio_kg: Optional[float]
    jet: Optional[int]
    es_reproceso: bool
    observaciones: Optional[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha"] = self.fecha.isoformat() if self.fecha else None
        d["fecha_terminado"] = self.fecha_terminado.isoformat() if self.fecha_terminado else None
        return d


@dataclass(frozen=True)
class StockProducto:
    """Stock actual de un producto químico, basado en la última lectura
    de inventario que se cargó (snapshot manual del operario).

    No suma ajustes ni compras posteriores a la lectura — replica la
    semántica baseline de formulas_app._current_stock_for_pricing.
    Si después se necesita el "stock teórico al día" (= última lectura
    + compras posteriores − consumido en órdenes posteriores + ajustes),
    es una query distinta que se agrega cuando se pida.
    """

    num: int                               # producto_num (PK estable)
    num_visible: int                       # número user-facing
    familia: str
    nombre: str
    unidad: str
    precio_us: float                       # productos.us (weighted-average)
    stock_kg: float                        # última lectura, 0.0 si nunca contado
    fecha_lectura: Optional[date]          # cuándo se contó por última vez
    nota: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha_lectura"] = self.fecha_lectura.isoformat() if self.fecha_lectura else None
        return d


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _parse_ddmmyyyy(s) -> Optional[date]:
    """'DD/MM/YYYY' → date. None si vacío/inválido."""
    if not s or not isinstance(s, str):
        return None
    try:
        d, m, y = s.strip().split("/")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _parse_iso(s) -> Optional[date]:
    """'YYYY-MM-DD' → date. None si vacío/inválido."""
    if not s:
        return None
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (ValueError, AttributeError):
        return None


def _f(v, default: float = 0.0) -> float:
    """Cast tolerante a float — None / '' / no-numérico → default."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fo(v) -> Optional[float]:
    """Cast tolerante a Optional[float] — preserva None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Función pública: tinturado resumen
# ---------------------------------------------------------------------------


def tinturado_resumen(
    limite: int = 500,
    solo_terminadas: bool = False,
    creacion_desde: Optional[date] = None,
    creacion_hasta: Optional[date] = None,
    terminado_desde: Optional[date] = None,
    terminado_hasta: Optional[date] = None,
) -> list[TinturadoOrden]:
    """Órdenes con kg crudo/terminado/desperdicio.

    No filtra por cliente (formulas_app no lo sabe). Dos rangos de fecha
    independientes — son preguntas distintas:

        creacion_*   → "órdenes que ENTRARON a tintorería en este período"
                       (filtra sobre ordenes.fecha, formato 'DD/MM/YYYY' legacy).
        terminado_*  → "órdenes que SALIERON terminadas en este período"
                       (filtra sobre ordenes.fecha_terminado, 'YYYY-MM-DD' ISO).

    Args:
        limite: máximo de filas a devolver, ordenadas por fecha (creación) DESC.
        solo_terminadas: si True, filtra a las que tienen `fecha_terminado IS NOT NULL`.
            False por default para no perder visibilidad de las que están en curso.
        creacion_desde / creacion_hasta: rango inclusivo sobre fecha de creación.
        terminado_desde / terminado_hasta: rango inclusivo sobre fecha_terminado.
            Implica solo_terminadas=True.

    Returns:
        Lista de TinturadoOrden ordenada por fecha (creación) DESC.
        [] si formulas_db no está configurado o si la query falla.
    """
    # Si pediste un rango de terminado, implícitamente excluís las no terminadas.
    if terminado_desde or terminado_hasta:
        solo_terminadas = True

    where = []
    params: list = []
    if solo_terminadas:
        where.append("o.fecha_terminado IS NOT NULL")
    if creacion_desde:
        where.append("TO_DATE(o.fecha, 'DD/MM/YYYY') >= %s")
        params.append(creacion_desde)
    if creacion_hasta:
        where.append("TO_DATE(o.fecha, 'DD/MM/YYYY') <= %s")
        params.append(creacion_hasta)
    if terminado_desde:
        where.append("o.fecha_terminado >= %s")
        params.append(terminado_desde.isoformat())
    if terminado_hasta:
        where.append("o.fecha_terminado <= %s")
        params.append(terminado_hasta.isoformat())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limite))

    rows = formulas_db.fetch_all(
        f"""
        SELECT
            o.numero,
            o.fecha,                    -- 'DD/MM/YYYY'
            o.fecha_terminado,          -- 'YYYY-MM-DD' (ISO, nullable)
            o.codigo                AS formula_cod,
            f.color,
            f.categoria,
            o.kil                   AS kilos_planeados,
            o.tela_cruda_kg,
            o.tela_terminada_kg,
            o.jet,
            COALESCE(o.es_reproceso, FALSE) AS es_reproceso,
            o.observaciones
          FROM ordenes o
          LEFT JOIN formulas f ON f.cod = o.codigo
         {where_sql}
         ORDER BY TO_DATE(o.fecha, 'DD/MM/YYYY') DESC NULLS LAST, o.id DESC
         LIMIT %s
        """,
        tuple(params),
    )
    return [_row_to_tinturado(r) for r in rows]


def desperdicio_periodo(
    desde: date, hasta: date, por: str = "terminado"
) -> dict:
    """Agregado de kg crudo/terminado/desperdicio en un rango de fechas.

    Dos modos según qué pregunta querés contestar:

        por="terminado" (default): filtra sobre `fecha_terminado`. Te dice
            "de las órdenes que SALIERON terminadas en este período, cuánto
            crudo entró, cuánto terminado salió, cuánto se perdió". Es la
            cifra estable — las órdenes en curso no aportan ruido.

        por="creacion": filtra sobre `fecha` (creación). Te dice "de las
            órdenes que ENTRARON a tintorería en este período, cuánto crudo
            ya se cargó, cuánto terminó saliendo, cuánto desperdicio
            acumulado tienen". Para órdenes todavía en curso, terminado_kg
            queda en NULL y no suma al total.

    Args:
        desde, hasta: rango inclusivo de fechas.
        por: "terminado" o "creacion".

    Returns:
        dict con ordenes_count, kilos_crudo_total, kilos_terminado_total,
        desperdicio_kg_total, desperdicio_pct (None si crudo=0).
    """
    if por == "creacion":
        where_sql = (
            "TO_DATE(fecha, 'DD/MM/YYYY') >= %s "
            "AND TO_DATE(fecha, 'DD/MM/YYYY') <= %s"
        )
        params: tuple = (desde, hasta)
    elif por == "terminado":
        where_sql = (
            "fecha_terminado IS NOT NULL "
            "AND fecha_terminado >= %s AND fecha_terminado <= %s"
        )
        params = (desde.isoformat(), hasta.isoformat())
    else:
        raise ValueError(f"por debe ser 'terminado' o 'creacion', no {por!r}")

    row = formulas_db.fetch_one(
        f"""
        SELECT
            COUNT(*)                            AS ordenes_count,
            COALESCE(SUM(tela_cruda_kg), 0)     AS crudo,
            COALESCE(SUM(tela_terminada_kg), 0) AS terminado
          FROM ordenes
         WHERE {where_sql}
        """,
        params,
    )
    if not row:
        return {
            "ordenes_count": 0,
            "kilos_crudo_total": 0.0,
            "kilos_terminado_total": 0.0,
            "desperdicio_kg_total": 0.0,
            "desperdicio_pct": None,
        }
    crudo = _f(row.get("crudo"))
    terminado = _f(row.get("terminado"))
    desperdicio = round(crudo - terminado, 3)
    pct = round(desperdicio / crudo * 100, 2) if crudo > 0 else None
    return {
        "ordenes_count": int(row.get("ordenes_count") or 0),
        "kilos_crudo_total": round(crudo, 3),
        "kilos_terminado_total": round(terminado, 3),
        "desperdicio_kg_total": desperdicio,
        "desperdicio_pct": pct,
    }


def _row_to_tinturado(r: dict) -> TinturadoOrden:
    cruda = _fo(r.get("tela_cruda_kg"))
    terminada = _fo(r.get("tela_terminada_kg"))
    desperdicio = None
    if cruda is not None and terminada is not None:
        desperdicio = round(cruda - terminada, 3)
    return TinturadoOrden(
        numero=str(r.get("numero") or ""),
        fecha=_parse_ddmmyyyy(r.get("fecha")),
        fecha_terminado=_parse_iso(r.get("fecha_terminado")),
        formula_cod=(r.get("formula_cod") or None),
        color=(r.get("color") or None),
        categoria=(r.get("categoria") or None),
        kilos_planeados=_f(r.get("kilos_planeados")),
        tela_cruda_kg=cruda,
        tela_terminada_kg=terminada,
        desperdicio_kg=desperdicio,
        jet=(int(r["jet"]) if r.get("jet") is not None else None),
        es_reproceso=bool(r.get("es_reproceso")),
        observaciones=(r.get("observaciones") or None),
    )


# ---------------------------------------------------------------------------
# Función pública: stock de químicos
# ---------------------------------------------------------------------------


def stock_quimicos() -> list[StockProducto]:
    """Stock actual por producto químico (última lectura de inventario).

    Productos sin lecturas aparecen igual con stock_kg=0 y fecha_lectura=None.
    Si necesitás filtrar solo los que tienen movimiento, hacelo en el caller.

    Returns:
        Lista de StockProducto ordenada por (familia, num_visible).
        [] si formulas_db no está configurado.
    """
    rows = formulas_db.fetch_all(
        """
        WITH ultima_lectura AS (
            SELECT DISTINCT ON (producto_num)
                   producto_num,
                   cantidad,
                   fecha,
                   nota
              FROM inventario
             ORDER BY producto_num, fecha DESC, id DESC
        )
        SELECT
            p.num,
            p.num_visible,
            p.familia,
            p.nombre,
            p.unidad,
            p.us                      AS precio_us,
            COALESCE(ul.cantidad, 0)  AS stock_kg,
            ul.fecha                  AS fecha_lectura,
            COALESCE(ul.nota, '')     AS nota
          FROM productos p
          LEFT JOIN ultima_lectura ul ON ul.producto_num = p.num
         ORDER BY p.familia, p.num_visible
        """
    )
    return [_row_to_stock(r) for r in rows]


def _row_to_stock(r: dict) -> StockProducto:
    return StockProducto(
        num=int(r.get("num") or 0),
        num_visible=int(r.get("num_visible") or 0),
        familia=str(r.get("familia") or ""),
        nombre=str(r.get("nombre") or ""),
        unidad=str(r.get("unidad") or ""),
        precio_us=_f(r.get("precio_us")),
        stock_kg=_f(r.get("stock_kg")),
        fecha_lectura=_parse_iso(r.get("fecha_lectura")),
        nota=str(r.get("nota") or ""),
    )


@dataclass(frozen=True)
class StockProductoAlDia:
    """Stock al día por producto: última lectura ± movimientos posteriores.

    Replica la fórmula de formulas_app:
        stock_al_dia = lectura_inicial
                     + ajustes posteriores a la lectura
                     + compras posteriores a la lectura
                     − consumo en órdenes terminadas posteriores a la lectura

    Notas:
        - El "consumo" es la suma de `orden_lineas.cantidad_kg` para órdenes
          con `fecha_terminado` posterior a la lectura. Aproximación: NO
          incluye los ajustes JSONB de `orden_lineas.ajustes` (las dosis
          sucesivas durante el tinturado). En la práctica son chicos
          comparados con el consumo base; si en algún momento se necesita
          la cifra exacta, se cambia esta query.
        - Si un producto no tiene lectura previa, lectura_inicial=0 y la
          fecha_lectura es None — el stock_al_dia es solo lo que sumaron
          compras y ajustes menos el consumo desde el día 0.
    """

    num: int
    num_visible: int
    familia: str
    nombre: str
    unidad: str
    precio_us: float
    lectura_kg: float                      # base: última lectura ≤ fecha
    fecha_lectura: Optional[date]
    ajustes_kg: float                      # suma de ajustes > fecha_lectura
    compras_kg: float                      # suma de compras > fecha_lectura
    consumo_kg: float                      # suma de consumo > fecha_lectura
    stock_al_dia_kg: float                 # = lectura + ajustes + compras − consumo

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha_lectura"] = self.fecha_lectura.isoformat() if self.fecha_lectura else None
        return d


def stock_quimicos_al_dia(fecha: Optional[date] = None) -> list[StockProductoAlDia]:
    """Stock al día por producto químico replicando la fórmula de formulas_app.

    Para cada producto:
        1. Última lectura de `inventario` con `fecha <= fecha_corte` (= lectura_kg).
        2. + suma de `inventario_ajustes.cantidad` con `fecha > fecha_lectura
              AND fecha <= fecha_corte` (= ajustes_kg).
        3. + suma de `compras.cantidad` con `fecha > fecha_lectura
              AND fecha <= fecha_corte` (= compras_kg).
        4. − suma de `orden_lineas.cantidad_kg` para órdenes con
              `fecha_terminado > fecha_lectura AND fecha_terminado <= fecha_corte`
              (= consumo_kg). NO incluye ajustes JSONB intra-línea.

    Args:
        fecha: corte de cálculo (incluido). Default hoy.

    Returns:
        Lista de StockProductoAlDia ordenada por (familia, num_visible).
        [] si formulas_db no está configurado.
    """
    from datetime import date as _date

    fecha_corte = (fecha or _date.today()).isoformat()

    rows = formulas_db.fetch_all(
        """
        WITH lectura AS (
            SELECT DISTINCT ON (producto_num)
                   producto_num,
                   cantidad AS lectura_kg,
                   fecha    AS fecha_lectura
              FROM inventario
             WHERE fecha <= %(corte)s
             ORDER BY producto_num, fecha DESC, id DESC
        ),
        ajustes AS (
            SELECT producto_num, SUM(cantidad) AS total
              FROM inventario_ajustes
             WHERE fecha <= %(corte)s
             GROUP BY producto_num
        ),
        ajustes_post AS (
            -- Solo los ajustes POSTERIORES a la lectura del mismo producto.
            -- Si no hay lectura, contamos todos los ajustes <= corte (lectura_base=0).
            SELECT ia.producto_num, SUM(ia.cantidad) AS total
              FROM inventario_ajustes ia
              LEFT JOIN lectura l ON l.producto_num = ia.producto_num
             WHERE ia.fecha <= %(corte)s
               AND (l.fecha_lectura IS NULL OR ia.fecha > l.fecha_lectura)
             GROUP BY ia.producto_num
        ),
        compras_post AS (
            SELECT c.producto_num, SUM(c.cantidad) AS total
              FROM compras c
              LEFT JOIN lectura l ON l.producto_num = c.producto_num
             WHERE c.fecha <= %(corte)s
               AND (l.fecha_lectura IS NULL OR c.fecha > l.fecha_lectura)
             GROUP BY c.producto_num
        ),
        consumo_post AS (
            -- Consumo solo de órdenes terminadas (replica el comportamiento
            -- de compute_consumption_by_date_terminado en formulas_app).
            -- NO incluye ajustes JSONB intra-línea — aproximación.
            SELECT ol.producto_num, SUM(ol.cantidad_kg) AS total
              FROM orden_lineas ol
              JOIN ordenes o ON o.id = ol.orden_id
              LEFT JOIN lectura l ON l.producto_num = ol.producto_num
             WHERE o.fecha_terminado IS NOT NULL
               AND o.fecha_terminado <= %(corte)s
               AND (l.fecha_lectura IS NULL OR o.fecha_terminado > l.fecha_lectura)
             GROUP BY ol.producto_num
        )
        SELECT
            p.num,
            p.num_visible,
            p.familia,
            p.nombre,
            p.unidad,
            p.us                                    AS precio_us,
            COALESCE(l.lectura_kg, 0)               AS lectura_kg,
            l.fecha_lectura                         AS fecha_lectura,
            COALESCE(ap.total, 0)                   AS ajustes_kg,
            COALESCE(cp.total, 0)                   AS compras_kg,
            COALESCE(cn.total, 0)                   AS consumo_kg,
            COALESCE(l.lectura_kg, 0)
              + COALESCE(ap.total, 0)
              + COALESCE(cp.total, 0)
              - COALESCE(cn.total, 0)               AS stock_al_dia_kg
          FROM productos p
          LEFT JOIN lectura      l  ON l.producto_num  = p.num
          LEFT JOIN ajustes_post ap ON ap.producto_num = p.num
          LEFT JOIN compras_post cp ON cp.producto_num = p.num
          LEFT JOIN consumo_post cn ON cn.producto_num = p.num
         ORDER BY p.familia, p.num_visible
        """,
        {"corte": fecha_corte},
    )
    return [_row_to_stock_al_dia(r) for r in rows]


def _row_to_stock_al_dia(r: dict) -> StockProductoAlDia:
    return StockProductoAlDia(
        num=int(r.get("num") or 0),
        num_visible=int(r.get("num_visible") or 0),
        familia=str(r.get("familia") or ""),
        nombre=str(r.get("nombre") or ""),
        unidad=str(r.get("unidad") or ""),
        precio_us=_f(r.get("precio_us")),
        lectura_kg=_f(r.get("lectura_kg")),
        fecha_lectura=_parse_iso(r.get("fecha_lectura")),
        ajustes_kg=_f(r.get("ajustes_kg")),
        compras_kg=_f(r.get("compras_kg")),
        consumo_kg=_f(r.get("consumo_kg")),
        stock_al_dia_kg=round(_f(r.get("stock_al_dia_kg")), 3),
    )


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------


def disponible() -> bool:
    """True si el bridge a formulas_app está configurado y respondiendo."""
    return formulas_db.disponible()
