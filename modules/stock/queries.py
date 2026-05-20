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

from datetime import date

import db


def _tarifas_mes_actual(ano: int, mes: int) -> dict:
    """TMT 2026-05-18 — Tarifas U$/kg canónicas del mes (legacy um/uk/uf/uq).

    scintela.iniciales tiene 4 columnas históricas con el precio U$/kg de
    cada etapa (provenientes de INFORMES.PRG line 337 del legacy):
      - um = U$/kg hilado (materia prima)
      - uk = U$/kg tejido
      - uf = U$/kg final (terminado / PT)
      - uq = U$/kg químicos (rara vez >0; químicos suelen ser valor neto)
    Estas tarifas reflejan el costo ponderado al momento del cierre del mes
    anterior (vía rollforward de cerrar_mes_auto), que es lo correcto para
    valuar el stock actual.

    Fallback: si la fila de iniciales del mes no tiene tarifas válidas,
    usar la más reciente que las tenga.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(um, 0)::float AS um,
               COALESCE(uk, 0)::float AS uk,
               COALESCE(uf, 0)::float AS uf,
               COALESCE(uq, 0)::float AS uq
          FROM scintela.iniciales
         WHERE yy = %s AND mesnum = %s
         LIMIT 1
        """,
        (ano, mes),
    )
    if row and (row["um"] or row["uk"] or row["uf"]):
        return dict(row)
    # Fallback: tarifas más recientes (alguna fila con datos).
    row = db.fetch_one(
        """
        SELECT COALESCE(um, 0)::float AS um,
               COALESCE(uk, 0)::float AS uk,
               COALESCE(uf, 0)::float AS uf,
               COALESCE(uq, 0)::float AS uq
          FROM scintela.iniciales
         WHERE COALESCE(um, 0) > 0 OR COALESCE(uk, 0) > 0 OR COALESCE(uf, 0) > 0
         ORDER BY yy DESC, mesnum DESC
         LIMIT 1
        """
    )
    return dict(row) if row else {"um": 0.0, "uk": 0.0, "uf": 0.0, "uq": 0.0}


def _opening_mes_actual(ano: int, mes: int) -> dict:
    """TMT 2026-05-18 — Opening del MES EN CURSO (no del año).

    scintela.iniciales tiene una fila por mes con el rollforward del
    cierre anterior. Para stock LIVE al día de hoy, partimos del opening
    del mes actual (= cierre del mes anterior) y agregamos delta del mes.

    Antes usábamos iniciales[mes=1] + delta YTD. Problema: scintela.tinto
    sólo tiene data del mes actual cargada (los meses históricos están
    vacíos en esa tabla — el tracking de tintura se hace fuera). Así que
    el delta YTD daba: opening_enero + compras_T_YTD + tinto_solo_mes_actual
    − facturas_YTD → terminado siempre negativo → clipped a 0.

    Con opening del mes actual + delta intra-mes, las queries son consistentes
    (todas miran al MISMO período corto).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(hilado, 0)    AS hilado,
               COALESCE(tejido, 0)    AS tejido,
               COALESCE(terminado, 0) AS terminado
          FROM scintela.iniciales
         WHERE yy = %s AND mesnum = %s
         LIMIT 1
        """,
        (ano, mes),
    )
    if row and (row["hilado"] or row["tejido"] or row["terminado"]):
        return {"hilado": float(row["hilado"]),
                "tejido": float(row["tejido"]),
                "terminado": float(row["terminado"])}
    # Fallback: iniciales más reciente con data.
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


def _compras_mes_actual_por_tipo(ano: int, mes: int) -> dict:
    """SUM(kg) + SUM(importe) de compras del MES en curso por tipo (H/K/T/Q).

    Filtrado al mes actual para mantener consistencia con _opening_mes_actual.
    """
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(COALESCE(tipo, ''))) AS tipo,
               SUM(COALESCE(kg, 0))            AS kg,
               SUM(COALESCE(importe, 0))       AS importe
          FROM scintela.compra
         WHERE COALESCE(stat, '') != 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         GROUP BY 1
        """,
        (ano, mes),
    ) or []
    return {r["tipo"]: {"kg": float(r["kg"] or 0),
                       "importe": float(r["importe"] or 0)}
            for r in rows}


def _compras_ytd_por_tipo(ano: int) -> dict:
    """SUM(kg)+SUM(importe) YTD por tipo — para U$/kg ponderado del año."""
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


def _facturas_kg_mes_actual(ano: int, mes: int) -> float:
    """SUM(kg) facturados del MES (kg que salieron del stock terminado)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(COALESCE(kg, 0)), 0) AS kg
          FROM scintela.factura
         WHERE COALESCE(stat, '') NOT IN ('X', 'Y')
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
        """,
        (ano, mes),
    )
    return float(row["kg"] or 0) if row else 0.0


def _tinto_kg_mes_actual(ano: int, mes: int) -> float:
    """kg tinturados (entran a terminado, salen de tejido) del MES."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(
            GREATEST(
                COALESCE(kg, 0),
                COALESCE(kgn, 0),
                COALESCE(toper, 0) + COALESCE(jersey, 0) + COALESCE(pique, 0)
              + COALESCE(messi, 0) + COALESCE(james, 0) + COALESCE(franela, 0)
              + COALESCE(j3, 0)    + COALESCE(jlyc, 0)  + COALESCE(flyc, 0)
              + COALESCE(falso, 0) + COALESCE(otros, 0) + COALESCE(kiana, 0)
            )
        ), 0) AS kg
          FROM scintela.tinto
         WHERE EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (ano, mes),
    )
    return float(row["kg"] or 0) if row else 0.0


def _tinto_kg_ytd(ano: int) -> float:
    """TMT 2026-05-18 — kg que entraron a TERMINADO via tintura del año.

    Flujo: tejido (K) → tintura → terminado.

    Fallback en cascada por columnas (la data legacy tiene `kg` poblado
    inconsistentemente; algunos registros sólo tienen las columnas
    individuales por tipo de tela):
      1) kg                         (consolidado bruto, si lo cargaron)
      2) kgn                        (consolidado neto)
      3) toper+jersey+pique+messi+james+franela+j3+jlyc+flyc+falso+otros+kiana
                                    (suma de columnas individuales)

    Sin filtro de stat (la data legacy rara vez lo setea en tinto).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(
            GREATEST(
                COALESCE(kg, 0),
                COALESCE(kgn, 0),
                COALESCE(toper, 0) + COALESCE(jersey, 0) + COALESCE(pique, 0)
              + COALESCE(messi, 0) + COALESCE(james, 0) + COALESCE(franela, 0)
              + COALESCE(j3, 0)    + COALESCE(jlyc, 0)  + COALESCE(flyc, 0)
              + COALESCE(falso, 0) + COALESCE(otros, 0) + COALESCE(kiana, 0)
            )
        ), 0) AS kg
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
    hoy = date.today()
    ano, mes = hoy.year, hoy.month

    # TMT 2026-05-18 v2 — Opening del MES en curso + delta INTRA-MES.
    # Antes usábamos opening de enero + delta YTD. Problema: scintela.tinto
    # sólo tiene data del mes actual cargada (los meses históricos están
    # vacíos — el tracking de tintura se hace fuera). Entonces el delta YTD
    # daba terminado negativo → clipped a 0. El opening del mes ya tiene
    # incorporado el rollforward de meses anteriores via cerrar_mes_auto.
    opening = _opening_mes_actual(ano, mes)
    comp_mes = _compras_mes_actual_por_tipo(ano, mes)
    tarifas = _tarifas_mes_actual(ano, mes)
    fact_kg = _facturas_kg_mes_actual(ano, mes)
    tinto_kg = _tinto_kg_mes_actual(ano, mes)

    # Flujo intra-mes:
    h_kg = opening["hilado"]    + comp_mes.get("H", {}).get("kg", 0.0)
    # Tejido: opening + compras K - tinturado (sale a tintura)
    k_kg = opening["tejido"]    + comp_mes.get("K", {}).get("kg", 0.0) - tinto_kg
    # Terminado: opening + compras T externas + tinturado − facturas
    t_kg = opening["terminado"] + comp_mes.get("T", {}).get("kg", 0.0) + tinto_kg - fact_kg

    # TMT 2026-05-18 v3 — Tarifas U$/kg canónicas de scintela.iniciales
    # (legacy um/uk/uf/uq). Antes usábamos weighted avg de compras YTD por
    # tipo, lo que daba: (a) Tejido 0.51 U$/kg porque compras K incluyen
    # producción (mucho kg, poco $); (b) Terminado 0 U$/kg porque no hay
    # compras T (no se compra terminado externamente, se produce).
    # Estas tarifas vienen del cierre anterior y reflejan el costo ponderado
    # correcto para valuar el stock actual.
    h_ukg = tarifas["um"]
    k_ukg = tarifas["uk"]
    t_ukg = tarifas["uf"]
    # Fallback defensivo: si la tarifa de terminado está en 0 pero hay
    # tarifa de tejido, usar la del tejido (el terminado nunca debería
    # valer menos que la materia prima de la que sale).
    if t_ukg <= 0 and k_ukg > 0:
        t_ukg = k_ukg

    # Químicos: valuación US$ del cierre del mes anterior (= scintela.iniciales.vq).
    # Si vq está en 0, fallback al último snapshot de scintela.historia.uqui.
    us_quim = 0.0
    row_vq = db.fetch_one(
        """
        SELECT COALESCE(vq, 0)::float AS vq
          FROM scintela.iniciales
         WHERE yy = %s AND mesnum = %s
         LIMIT 1
        """,
        (ano, mes),
    )
    if row_vq and row_vq["vq"]:
        us_quim = float(row_vq["vq"])
    else:
        try:
            from modules.informes import queries as inf
            hist_live = inf.historia_ultimo_snapshot() or {}
            us_quim = float(hist_live.get("uqui") or 0)
        except Exception:
            us_quim = 0.0

    # TMT 2026-05-18 — Kg de químicos derivado: el sistema legacy guarda
    # químicos sólo en US$ (vq), pero podemos derivar kg si tenemos la
    # tarifa uq de iniciales. Sino, sumamos kg de compras tipo Q del año.
    q_ukg = float(tarifas.get("uq") or 0)
    q_kg = 0.0
    if q_ukg > 0 and us_quim > 0:
        q_kg = us_quim / q_ukg
    else:
        # Fallback: kg de compras Q YTD si están cargadas
        comp_ytd = _compras_ytd_por_tipo(ano)
        q_kg = comp_ytd.get("Q", {}).get("kg", 0.0)
        if q_kg > 0 and us_quim > 0:
            q_ukg = us_quim / q_kg

    stock = {
        "hilado":    {"kg": max(0.0, h_kg), "ukg": h_ukg, "us": max(0.0, h_kg) * h_ukg},
        "tejido":    {"kg": max(0.0, k_kg), "ukg": k_ukg, "us": max(0.0, k_kg) * k_ukg},
        "terminado": {"kg": max(0.0, t_kg), "ukg": t_ukg, "us": max(0.0, t_kg) * t_ukg},
        "quimicos":  {"kg": max(0.0, q_kg), "ukg": q_ukg, "us": us_quim},
    }
    total_kg = stock["hilado"]["kg"] + stock["tejido"]["kg"] + stock["terminado"]["kg"]
    total_us = stock["hilado"]["us"] + stock["tejido"]["us"] + stock["terminado"]["us"]
    stock["total"] = {
        "kg":  total_kg,
        "us":  total_us,
        "ukg": (total_us / total_kg) if total_kg > 0 else 0.0,
    }
    stock["total_con_quimicos"] = total_us + us_quim
    stock["snapshot_fecha"] = f"opening {ano}-{mes:02d} + flujo intra-mes"
    return stock


def compras_mes_por_tipo(meses_atras: int = 3) -> list[dict]:
    """Resumen de compras de los últimos N meses agrupadas por tipo.

    Devuelve filas {tipo, n_compras, kg_total, importe_total} ordenadas
    por importe descendente. Filtra compras anuladas (stat='Y').

    Sirve para que la dueña vea el "qué entró últimamente" de un vistazo,
    como contexto del stock actual.
    """
    # TMT 2026-05-20 PASADA 3 — month math fix.
    # Antes: `today.replace(day=1) - timedelta(days=meses*31)` salta más
    # días de los que debería cuando hay meses cortos (ej. dic-feb).
    # Ahora retrocedemos mes a mes para landing exacto en el día 1.
    _hoy = date.today().replace(day=1)
    _yr, _mo = _hoy.year, _hoy.month
    _mo -= meses_atras
    while _mo <= 0:
        _mo += 12
        _yr -= 1
    desde = date(_yr, _mo, 1)
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
