"""Queries para la pantalla de Stock.

Anteriormente esto delegaba a `informes.queries.informe_balance().stock`,
pero ese cálculo lee de `historia.{hilado,tejido,terminado}` que NO existen
como columnas — siempre cae al fallback de `iniciales` que es el opening
del año y queda en 0 si el opening no se cargó. Resultado: /stock todo
en 0 aunque la dueña tenga 32 compras de Hilado en los últimos 3 meses.

La nueva versión computa el stock live directamente desde `scintela.compra`
y `scintela.factura`:

  - Opening del año (1° de enero) viene de `iniciales` si está cargado, 0 si no.
  - SUM(compra.kg) por tipo del año en curso (anuladas excluidas).
  - Para Terminado: + compras tipo='T' − facturas (kg vendidos del año).
  - U$/kg ponderado: SUM(importe) / SUM(kg) del año.

Químicos no tiene kg — sigue siendo escalar US$ del último snapshot.
TMT 2026-05-13.
"""
from __future__ import annotations

from datetime import date, timedelta

import db


def _opening_inicial(ano: int) -> dict:
    """Opening del año desde iniciales (mes 1, año actual o el más reciente)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(hilado, 0)    AS hilado,
               COALESCE(tejido, 0)    AS tejido,
               COALESCE(terminado, 0) AS terminado
          FROM scintela.iniciales
         WHERE yy = %s AND mesnum = 1
         LIMIT 1
        """,
        (ano,),
    )
    if row and (row["hilado"] or row["tejido"] or row["terminado"]):
        return {"hilado": float(row["hilado"]),
                "tejido": float(row["tejido"]),
                "terminado": float(row["terminado"])}
    # Fallback: cualquier fila de iniciales con datos (la más reciente).
    row = db.fetch_one(
        """
        SELECT COALESCE(hilado, 0)    AS hilado,
               COALESCE(tejido, 0)    AS tejido,
               COALESCE(terminado, 0) AS terminado
          FROM scintela.iniciales
         WHERE COALESCE(hilado,0) > 0
            OR COALESCE(tejido,0) > 0
            OR COALESCE(terminado,0) > 0
         ORDER BY yy DESC, mesnum DESC
         LIMIT 1
        """
    )
    if row:
        return {"hilado": float(row["hilado"]),
                "tejido": float(row["tejido"]),
                "terminado": float(row["terminado"])}
    return {"hilado": 0.0, "tejido": 0.0, "terminado": 0.0}


def _compras_ytd_por_tipo(ano: int) -> dict:
    """SUM(kg) + SUM(importe) de compras del año por tipo (H/K/T/Q).

    Excluye anuladas (stat='Y'). Devuelve dict {tipo: {"kg": x, "importe": y}}.
    """
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(COALESCE(tipo, ''))) AS tipo,
               SUM(COALESCE(kg, 0))            AS kg,
               SUM(COALESCE(importe, 0))       AS importe
          FROM scintela.compra
         WHERE COALESCE(stat, '') != 'Y'
           AND EXTRACT(YEAR FROM fecha) = %s
         GROUP BY 1
        """,
        (ano,),
    ) or []
    return {r["tipo"]: {"kg": float(r["kg"] or 0),
                       "importe": float(r["importe"] or 0)}
            for r in rows}


def _facturas_kg_ytd(ano: int) -> float:
    """SUM(kg) facturados del año (kg que salieron del stock terminado)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(COALESCE(kg, 0)), 0) AS kg
          FROM scintela.factura
         WHERE COALESCE(stat, '') NOT IN ('X', 'Y')
           AND EXTRACT(YEAR FROM fecha) = %s
        """,
        (ano,),
    )
    return float(row["kg"] or 0) if row else 0.0


def _tinto_kg_ytd(ano: int) -> float:
    """TMT 2026-05-18 — kg que entraron a TERMINADO via tintura del año.

    El flujo es: tejido (K) → tintura → terminado. Cada operación de
    tintura suma kg al stock de terminado y los resta del stock de tejido.

    En scintela.tinto, cada fila es una orden de tintura. El total de kg
    es la suma de las columnas de tipos (toper+jersey+pique+messi+james+
    franela+otros+etc.) — pero ya tienen las columnas `kg` y `kgn` (kg
    netos) consolidadas. Usamos `kg` (bruto).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(COALESCE(kg, 0)), 0) AS kg
          FROM scintela.tinto
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (ano,),
    )
    return float(row["kg"] or 0) if row else 0.0


def resumen_stock() -> dict:
    """Devuelve el dict canónico de stock para la página /stock.

    Shape:
        {
          "hilado":    {"kg": ..., "ukg": ..., "us": ...},
          "tejido":    {"kg": ..., "ukg": ..., "us": ...},
          "terminado": {"kg": ..., "ukg": ..., "us": ...},
          "quimicos":  {"kg": 0,   "ukg": 0,   "us": ...},  # solo $
          "total":     {"kg": ..., "ukg": ..., "us": ...},  # sin químicos
          "total_con_quimicos": float,
          "snapshot_fecha": "YYYY-MM" o None,
        }
    """
    ano = date.today().year
    opening = _opening_inicial(ano)
    comp = _compras_ytd_por_tipo(ano)
    fact_kg = _facturas_kg_ytd(ano)
    # TMT 2026-05-18 — flujo de tejido → terminado via tintura intra-año.
    # Antes la fórmula era: terminado = opening_terminado + compras_T − facturas.
    # Eso daba 0 cuando facturas YTD > opening + compras_T (siempre, porque
    # el terminado se PRODUCE internamente, no se compra). Fix: sumar
    # kg tinturados del año (scintela.tinto.kg) — esa es la producción
    # que entra a terminado.
    tinto_kg = _tinto_kg_ytd(ano)

    # KG live = opening + compras YTD del tipo. Para terminado restamos
    # las facturas vendidas (el dBase legacy hacía lo mismo en el screen
    # MAT.PR del INFORMES.PRG).
    h_kg = opening["hilado"]   + comp.get("H", {}).get("kg", 0.0)
    # Tejido: opening + compras K - tinturado (sale a terminado)
    k_kg = opening["tejido"]   + comp.get("K", {}).get("kg", 0.0) - tinto_kg
    # Terminado: opening + compras T (raras, externas) + tinturado − facturas
    t_kg = opening["terminado"] + comp.get("T", {}).get("kg", 0.0) + tinto_kg - fact_kg

    # U$/kg ponderado = importe YTD / kg YTD por tipo. Si kg=0, 0.0.
    def _ukg(tipo):
        c = comp.get(tipo, {})
        kg = c.get("kg", 0.0)
        if kg > 0:
            return c.get("importe", 0.0) / kg
        return 0.0

    h_ukg = _ukg("H")
    k_ukg = _ukg("K")
    t_ukg = _ukg("T")

    # Químicos — escalar US$ del último snapshot (no hay kg trackeados).
    try:
        from modules.informes import queries as inf
        hist_live = inf.historia_ultimo_snapshot() or {}
        us_quim = float(hist_live.get("uqui") or 0)
    except Exception:
        us_quim = 0.0

    stock = {
        "hilado":    {"kg": max(0.0, h_kg), "ukg": h_ukg, "us": max(0.0, h_kg) * h_ukg},
        "tejido":    {"kg": max(0.0, k_kg), "ukg": k_ukg, "us": max(0.0, k_kg) * k_ukg},
        "terminado": {"kg": max(0.0, t_kg), "ukg": t_ukg, "us": max(0.0, t_kg) * t_ukg},
        "quimicos":  {"kg": 0.0, "ukg": 0.0, "us": us_quim},
    }
    total_kg = stock["hilado"]["kg"] + stock["tejido"]["kg"] + stock["terminado"]["kg"]
    total_us = stock["hilado"]["us"] + stock["tejido"]["us"] + stock["terminado"]["us"]
    stock["total"] = {
        "kg":  total_kg,
        "us":  total_us,
        "ukg": (total_us / total_kg) if total_kg > 0 else 0.0,
    }
    stock["total_con_quimicos"] = total_us + us_quim
    stock["snapshot_fecha"] = f"opening {ano}-01 + compras/facturas YTD"
    return stock


def compras_mes_por_tipo(meses_atras: int = 3) -> list[dict]:
    """Resumen de compras de los últimos N meses agrupadas por tipo.

    Devuelve filas {tipo, n_compras, kg_total, importe_total} ordenadas
    por importe descendente. Filtra compras anuladas (stat='Y').

    Sirve para que la dueña vea el "qué entró últimamente" de un vistazo,
    como contexto del stock actual.
    """
    desde = (date.today().replace(day=1) - timedelta(days=meses_atras * 31))
    desde = desde.replace(day=1)
    return db.fetch_all(
        """
        SELECT COALESCE(NULLIF(TRIM(UPPER(c.tipo)), ''), '?') AS tipo,
               COUNT(*)              AS n_compras,
               SUM(COALESCE(c.kg, 0))      AS kg_total,
               SUM(COALESCE(c.importe, 0)) AS importe_total
          FROM scintela.compra c
         WHERE c.stat IS DISTINCT FROM 'Y'
           AND c.fecha >= %s
         GROUP BY 1
         ORDER BY importe_total DESC
        """,
        (desde,),
    ) or []


# Vocabulario humano de los tipos legacy del dBase.
TIPO_LABEL = {
    "H": "Hilado",
    "K": "Tejido",
    "T": "Tintura / Terminado",
    "Q": "Químicos",
    "A": "Anticipo",
    "C": "Otros",
    "M": "Maquinaria",
    "I": "Insumo",
    "S": "Servicio",
}


def label_tipo(t: str) -> str:
    return TIPO_LABEL.get((t or "").upper().strip()[:1], t or "Sin tipo")
