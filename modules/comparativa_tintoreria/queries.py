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
