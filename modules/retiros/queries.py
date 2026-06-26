"""Queries de retiros del dueño (scintela.retiros)."""
from datetime import date, timedelta

import db
from filters import today_ec

# Marcador de origen PC para los retiros que NO viven en el dBase (caso
# "retiro OP banco USA"). El sync (import_dbf) preserva estas filas al
# re-cargar RETIROS.DBF en vez de pisarlas. TMT 2026-06-26.
USUARIO_RETIRO_OP = "pc-retiro-op"


def saldo_op() -> dict:
    """Saldo OP (over-price/aporte) que se ve en posdatados + contexto de retiros.

    Mecánica dBase (verificada en COMPRAS/POSDAT/RETIROS):
      - El crédito OP entra como compra/posdat NEGATIVA a prov='OP' (un pasivo
        negativo). El "saldo OP" que la dueña mira en posdatados = ese crédito
        ABIERTO (posdat banc=0). Es un STOCK (lo vigente ahora).
      - El pago a accionistas es un RETIRO con de='OP' (un FLUJO; se acumula en
        /retiros). NO netear el flujo histórico contra el stock vigente: los
        retiros OP arrastran años (>6M) y el crédito abierto es chico → un neto
        ingenuo da un número sin sentido.

    Devuelve POSITIVOS legibles:
      credito        = |Σ posdat OP abierto|  → el "Saldo OP" de posdatados.
      retirado_anio  = Σ retiros OP del año en curso (contexto).
      retirado_total = Σ retiros OP histórico (referencia).
    """
    pos = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS s
          FROM scintela.posdat
         WHERE UPPER(TRIM(prov)) = 'OP'
           AND COALESCE(banc, 0) = 0
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or {"s": 0}
    ret_anio = db.fetch_one(
        "SELECT COALESCE(SUM(ret), 0) AS s FROM scintela.retiros "
        "WHERE UPPER(TRIM(de)) = 'OP' "
        "  AND EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM CURRENT_DATE)"
    ) or {"s": 0}
    ret_total = db.fetch_one(
        "SELECT COALESCE(SUM(ret), 0) AS s FROM scintela.retiros "
        "WHERE UPPER(TRIM(de)) = 'OP'"
    ) or {"s": 0}
    posdat_op = float(pos["s"] or 0)        # negativo (crédito)
    return {
        "posdat_op": round(posdat_op, 2),
        "credito": round(-posdat_op, 2),    # |crédito| abierto = Saldo OP
        "retirado_anio": round(float(ret_anio["s"] or 0), 2),
        "retirado_total": round(float(ret_total["s"] or 0), 2),
    }


def crear_op(*, monto: float, de: str = "OP", fecha: date | None = None,
             concepto: str | None = None, usuario: str = "web") -> dict:
    """Registra un retiro a accionistas contra el saldo OP ("banco USA").

    Espejo del retiro OP del dBase (RETIROS DE='OP', concepto 'RR OP … B.1'),
    pero el dinero sale de un banco en USA que NO está en el programa: por eso
    NO se crea movimiento bancario (nb=NULL) y la leyenda 'banco USA' es sólo
    un comentario. Baja el saldo OP (vía el neteo de saldo_op) y queda en
    /retiros como retiro de accionista. Auditado en mov_doble. Reversible
    anulando/borrando el retiro.
    """
    monto = round(float(monto or 0), 2)
    if monto <= 0:
        raise ValueError("El monto del retiro debe ser mayor que cero.")
    de = (de or "OP").strip().upper()[:5] or "OP"
    fecha = fecha or today_ec()
    if not concepto:
        concepto = f"RR {de} banco USA" if de != "OP" else "RR OP banco USA"
    concepto = concepto[:100]

    with db.tx() as conn:
        row = db.execute_returning(
            """
            INSERT INTO scintela.retiros
                (fecha, nb, ret, de, concepto, clave, usuario_crea)
            VALUES (%s, NULL, %s, %s, %s, NULL, %s)
            RETURNING id_retiro
            """,
            (fecha, monto, de, concepto, USUARIO_RETIRO_OP),
            conn=conn,
        ) or {}
        id_retiro = int(row.get("id_retiro") or 0)
        try:
            import mov_doble as _md
            _md.registrar(
                conn=conn,
                tipo="retiro_op",
                origen_table="retiros",
                origen_id=id_retiro,
                destino_table="retiros",
                destino_id=id_retiro,
                importe=monto,
                fecha=fecha,
                concepto=f"Retiro OP a accionistas (banco USA) — {de} $ {monto:.2f}"[:200],
                usuario=usuario,
                metadata={"de": de, "concepto": concepto, "origen": "retiro_op_banco_usa"},
            )
        except Exception:
            # El retiro necesita huella en /historial; si mov_doble explota por
            # algo inesperado, abortamos para no dejar el retiro sin auditar.
            raise
    return {"id_retiro": id_retiro, "monto": monto, "de": de, "concepto": concepto}


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
    desde_d = desde or (today_ec() - timedelta(days=365)).isoformat()
    hasta_d = hasta or today_ec().isoformat()
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
    desde_d = desde or (today_ec() - timedelta(days=90)).isoformat()
    hasta_d = hasta or today_ec().isoformat()
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
