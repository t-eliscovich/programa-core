"""Queries de retiros del dueño (scintela.retiros)."""
from datetime import date, timedelta

import db


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    de: str | None = None,
    limite: int = 500,
) -> list[dict]:
    """Histórico de retiros filtrable por concepto/de + fecha + banco."""
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto, r.clave,
               r.id_transaccion_bancaria,
               COALESCE(b.nombre, '') AS banco
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(r.concepto,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(r.de,'')) LIKE UPPER(%(like)s))
          AND (%(de)s IS NULL OR UPPER(r.de) = UPPER(%(de)s))
          AND (%(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
        ORDER BY r.fecha DESC, r.id_retiro DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like, "de": de or None,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    ) or []


def totales_por_persona(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """Cuánto retiró cada socio en el periodo. Útil para informe trimestral."""
    desde_d = desde or (date.today() - timedelta(days=365)).isoformat()
    hasta_d = hasta or date.today().isoformat()
    return db.fetch_all(
        """
        SELECT COALESCE(de, '(sin asignar)') AS de,
               SUM(ret)                       AS total,
               COUNT(*)                       AS n_retiros,
               MAX(fecha)                     AS ultimo
        FROM scintela.retiros
        WHERE fecha BETWEEN %s::date AND %s::date
        GROUP BY 1
        ORDER BY total DESC
        """,
        (desde_d, hasta_d),
    ) or []


def totales_por_mes(meses: int = 12) -> list[dict]:
    """Tendencia mensual."""
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               SUM(ret) AS total,
               COUNT(*) AS n
        FROM scintela.retiros
        WHERE fecha >= CURRENT_DATE - (%s || ' months')::interval
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        (max(1, min(int(meses or 12), 60)),),
    ) or []


def resumen(desde: str | None = None, hasta: str | None = None) -> dict:
    """Total + n del filtro actual."""
    desde_d = desde or (date.today() - timedelta(days=90)).isoformat()
    hasta_d = hasta or date.today().isoformat()
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0)        AS total,
               COUNT(*)                      AS n,
               COUNT(DISTINCT de)            AS n_personas
        FROM scintela.retiros
        WHERE fecha BETWEEN %s::date AND %s::date
        """,
        (desde_d, hasta_d),
    ) or {}
    n = int(row.get("n") or 0)
    total = float(row.get("total") or 0)
    return {
        "n":               n,
        "n_personas":      int(row.get("n_personas") or 0),
        "total":           total,
        "ticket_promedio": (total / n) if n else 0.0,
        "desde":           desde_d,
        "hasta":           hasta_d,
    }
