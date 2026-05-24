"""Queries para /informes/comparativa-tintoreria.

Solo PC (scintela.tinto). El lado formulas_app lo provee
`modules.tintura.service.tinturado_resumen`.
"""
from __future__ import annotations

from datetime import date

import db


def tinto_pc_por_dia_color(desde: date, hasta: date) -> list[dict]:
    """Agregado de scintela.tinto por (fecha, cod) en el rango [desde, hasta].

    Columnas devueltas:
        fecha     — date
        cod       — código corto del color (ROJ, AZL, LAV, …). Vacío → 'S/COD'.
        kg        — SUM(kgn) preferido, fallback a SUM(kg). Excluye stat X/Y.
        importe   — SUM(importe) USD
        n_lineas  — COUNT(*)

    No matchea órdenes individuales (scintela.tinto NO tiene OT directo).
    Ordenado por fecha DESC, cod ASC.
    """
    return db.fetch_all(
        """
        SELECT fecha,
               UPPER(TRIM(COALESCE(NULLIF(cod, ''), 'S/COD'))) AS cod,
               COALESCE(SUM(GREATEST(COALESCE(kgn, 0), COALESCE(kg, 0))), 0) AS kg,
               COALESCE(SUM(importe), 0)                                    AS importe,
               COUNT(*)                                                     AS n_lineas
          FROM scintela.tinto
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
         GROUP BY fecha, UPPER(TRIM(COALESCE(NULLIF(cod, ''), 'S/COD')))
         ORDER BY fecha DESC, cod ASC
        """,
        (desde, hasta),
    )


def tinto_bajos_fuertes_por_mes(desde: date, hasta: date, limite_bajos: float = 0.4) -> list[dict]:
    """Resumen mes-por-mes con clasificación Bajos vs Fuertes.

    Regla: una línea de scintela.tinto es "Bajos" cuando importe/kg <= limite_bajos
    (default 0.4 US/kg). Si no, es "Fuertes". Las líneas sin kg o sin importe
    se ignoran para clasificar (kg=0 → no se puede dividir).

    Devuelve una fila por (yy, mm, tipo) con SUM(kgn) y SUM(importe).
    El caller arma la tabla cruzada (Bajos/Fuertes/Total) con porcentajes.

    Excluye stat X (eliminados) e Y (anulados) como el resto del módulo.
    """
    return db.fetch_all(
        """
        WITH clasif AS (
            SELECT EXTRACT(YEAR  FROM fecha)::int AS yy,
                   EXTRACT(MONTH FROM fecha)::int AS mm,
                   COALESCE(kgn, kg, 0)::numeric  AS kg_n,
                   COALESCE(importe, 0)::numeric  AS imp,
                   CASE
                     WHEN COALESCE(importe, 0) / NULLIF(kg, 0) <= %s THEN 'Bajos'
                     ELSE 'Fuertes'
                   END AS tipo
              FROM scintela.tinto
             WHERE COALESCE(stat, '') NOT IN ('X', 'Y')
               AND COALESCE(kg, 0) > 0
               AND fecha BETWEEN %s AND %s
        )
        SELECT yy, mm, tipo,
               COALESCE(SUM(kg_n), 0) AS kg,
               COALESCE(SUM(imp), 0)  AS importe
          FROM clasif
         GROUP BY yy, mm, tipo
         ORDER BY yy, mm, tipo
        """,
        (limite_bajos, desde, hasta),
    )


def _amortizacion_dcc_por_mes(desde: date, hasta: date) -> dict:
    """Replica DCC = deprmaq + depract*0.5 (amortización tintorería) mes-por-mes.

    Aproximación: usa la cuota mensual ACTUAL de cada activo (la columna
    `scintela.activos.cuota`) y la aplica a cada mes del rango. Asume que
    los activos existían y depreciaban con la misma cuota durante todo el
    rango — para periodos cortos (12 meses) suele ser razonable.

    Para el mes actual: prorratea por día (COEF = min(día, 30)/30), igual
    que la función `amortizaciones_mensuales()` de informes/queries.py.
    Para meses pasados: cuota completa (COEF = 1.0).
    """
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(tipo)) AS tipo,
               COALESCE(SUM(cuota), 0) AS total
          FROM scintela.activos
         WHERE COALESCE(cuota, 0) > 0
         GROUP BY 1
        """
    ) or []
    by = {r.get("tipo"): float(r.get("total") or 0) for r in rows}
    deprmaq = by.get("M", 0.0)
    depract = by.get("I", 0.0)
    dcc_full = deprmaq + depract * 0.5

    from datetime import date as _date
    hoy = _date.today()
    res: dict = {}
    cur_yy, cur_mm = desde.year, desde.month
    end_yy, end_mm = hasta.year, hasta.month
    while (cur_yy, cur_mm) <= (end_yy, end_mm):
        if cur_yy == hoy.year and cur_mm == hoy.month:
            coef = min(hoy.day, 30) / 30.0
            res[(cur_yy, cur_mm)] = dcc_full * coef
        else:
            res[(cur_yy, cur_mm)] = dcc_full
        cur_mm += 1
        if cur_mm > 12:
            cur_mm = 1
            cur_yy += 1
    return res


def gs_produccion_tintoreria_por_mes(desde: date, hasta: date) -> dict:
    """Replica "Gs. Producción Tintorería" = `_gs_tin` de informes/queries.py
    mes-por-mes — IDÉNTICO al cálculo de Gastos del mes (TINTORERÍA TOTAL
    CON AMORT) para que las dos pantallas muestren el mismo número:
        = V4 + V5 + V6 (= xgast num 4/5/6  +  compras C/Q/T mapeadas)
        + amortización DCC (= deprmaq + depract * 0.5)

    TMT 2026-05-24 v3 — Bug detectado por dueña: la tabla mensual mostraba
    $241.238 en vez de $323.786 (que es lo que muestra Gastos del mes).
    Causa: el cálculo de Gastos del mes (gastos_xgast_v1_a_v9_mes) suma
    xgast + COMPRAS mapeadas a num 4/5/6 vía _SQL_COMPRA_NUM_CASE (tipo
    C/Q/T con concepto = sueldos/servicios/otros). Acá solo sumaba xgast
    y faltaban las compras → faltaban $82.548 (la diferencia).
    Fix: importar el mismo CASE y agregarlo a la query mensual.

    Devuelve dict {(yy, mm): total_us}. Meses sin gastos devuelven el DCC
    (porque la amortización corre aunque no haya xgast).
    """
    # Import local para evitar circular y porque solo necesitamos el
    # snippet SQL (no la función completa).
    from modules.informes.queries import _SQL_COMPRA_NUM_CASE

    out: dict = {}

    # 1) xgast num 4/5/6
    rows_xg = db.fetch_all(
        """
        SELECT EXTRACT(YEAR  FROM fecha)::int  AS yy,
               EXTRACT(MONTH FROM fecha)::int  AS mm,
               COALESCE(SUM(importe), 0)::float AS total
          FROM scintela.xgast
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
           AND COALESCE(num, 0) IN (4, 5, 6)
         GROUP BY yy, mm
        """,
        (desde, hasta),
    )
    for r in rows_xg:
        out[(int(r["yy"]), int(r["mm"]))] = float(r["total"] or 0)

    # 2) compras mapeadas a num 4/5/6 vía el mismo CASE que usa el live.
    sql_cp = f"""
        SELECT EXTRACT(YEAR  FROM c.fecha)::int  AS yy,
               EXTRACT(MONTH FROM c.fecha)::int  AS mm,
               COALESCE(SUM(c.importe), 0)::float AS total
          FROM scintela.compra c
         WHERE c.fecha BETWEEN %s AND %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND ({_SQL_COMPRA_NUM_CASE}) IN (4, 5, 6)
         GROUP BY yy, mm
    """
    rows_cp = db.fetch_all(sql_cp, (desde, hasta)) or []
    for r in rows_cp:
        k = (int(r["yy"]), int(r["mm"]))
        out[k] = out.get(k, 0.0) + float(r["total"] or 0)

    # 3) amortización DCC por mes (= deprmaq + depract*0.5)
    try:
        dcc = _amortizacion_dcc_por_mes(desde, hasta)
        for k, v in dcc.items():
            out[k] = out.get(k, 0.0) + v
    except Exception:
        pass

    return out


def tinto_pc_por_dia(desde: date, hasta: date) -> list[dict]:
    """Agregado de scintela.tinto por fecha (sin desglosar color).

    Útil para la fila de totales/sub-totales por día.
    """
    return db.fetch_all(
        """
        SELECT fecha,
               COALESCE(SUM(GREATEST(COALESCE(kgn, 0), COALESCE(kg, 0))), 0) AS kg,
               COALESCE(SUM(importe), 0)                                    AS importe,
               COUNT(*)                                                     AS n_lineas
          FROM scintela.tinto
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
         GROUP BY fecha
         ORDER BY fecha DESC
        """,
        (desde, hasta),
    )
