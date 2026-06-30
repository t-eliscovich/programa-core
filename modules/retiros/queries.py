"""Queries de retiros del dueño (scintela.retiros)."""
from datetime import date, timedelta

import db
from filters import today_ec

# Marcador de origen PC para los retiros que NO viven en el dBase (caso
# "retiro OP banco USA"). El sync (import_dbf) preserva estas filas al
# re-cargar RETIROS.DBF en vez de pisarlas. TMT 2026-06-26.
USUARIO_RETIRO_OP = "pc-retiro-op"


def saldo_op() -> dict:
    """Saldo OP (over-price/aporte): crédito de las COMPRAS OP menos lo retirado.

    Mecánica verificada en el dBase real (COMPRAS/RETIROS, 2026-06-26): NO se
    baja nada a mano (0 compras OP positivas, 0 ediciones). El saldo OP se
    NETEA solo: el crédito entra como compra NEGATIVA a prov='OP' y el pago a
    accionistas como RETIRO de='OP'; el saldo = Σ(compras OP) + Σ(retiros OP)
    del MISMO período. Cada retiro sube el lado positivo → el saldo baja solo.

    Scope correcto: las compras OP retenidas (scintela.compra mirrorea el DBF,
    que purga las viejas) + los retiros OP DESDE la primera compra retenida
    (los retiros anteriores cancelaban créditos ya purgados). Así el neto es
    el crédito que TODAVÍA falta retirar. Es sólo un display: no toca el
    balance (las compras ya están en TOTP y los retiros en URET).

    Devuelve POSITIVOS legibles:
      credito    = |Σ compras OP|                         (crédito cargado)
      retirado   = Σ retiros OP desde la 1ª compra OP      (lo ya pagado)
      disponible = credito − retirado                      (lo que falta retirar; BAJA con cada retiro)
    """
    comp = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS s, MIN(fecha) AS d
          FROM scintela.compra
         WHERE UPPER(TRIM(codigo_prov)) = 'OP'
           AND COALESCE(stat, '') <> 'Y'
        """
    ) or {"s": 0, "d": None}
    credito = -float(comp["s"] or 0)          # |crédito| en positivo
    desde = comp.get("d")
    if desde is not None:
        ret = db.fetch_one(
            "SELECT COALESCE(SUM(ret), 0) AS s FROM scintela.retiros "
            "WHERE UPPER(TRIM(de)) = 'OP' AND fecha >= %s",
            (desde,),
        ) or {"s": 0}
    else:
        ret = {"s": 0}
    retirado = float(ret["s"] or 0)
    return {
        "credito": round(credito, 2),
        "retirado": round(retirado, 2),
        "disponible": round(credito - retirado, 2),
        "desde": desde,
    }


def lineas_op() -> list[dict]:
    """Lineas OP registradas (creditos) con su saldo restante por linea.

    Une las DOS fuentes de credito OP:
      - compras OP (scintela.compra, codigo_prov='OP', importe negativo): el
        over-price de cada importacion (lo que forma el Saldo OP del panel).
      - la(s) fila(s) OP de posdatados (scintela.posdat, prov='OP').
    Por linea:
      credito  = |importe|  (positivo)
      retirado = Sum op_retiro_linea imputado a esa linea (SOLO display, no balance)
      restante = credito - retirado   (BAJA al retirar desde esa linea)
    line_key estable a traves del Sync dBase (numero/num + concepto del DBF).
    """
    rows = db.fetch_all(
        """
        SELECT 'C|' || COALESCE(numero, 0) || '|' || COALESCE(concepto, '') AS line_key,
               'compra'                AS origen,
               fecha,
               COALESCE(concepto, '')  AS concepto,
               -COALESCE(importe, 0)   AS credito
          FROM scintela.compra
         WHERE UPPER(TRIM(codigo_prov)) = 'OP'
           AND COALESCE(stat, '') <> 'Y'
        UNION ALL
        SELECT 'P|' || COALESCE(num, 0) || '|' || COALESCE(concepto, '') AS line_key,
               'posdat'                AS origen,
               fecha,
               COALESCE(concepto, '')  AS concepto,
               -COALESCE(importe, 0)   AS credito
          FROM scintela.posdat
         WHERE UPPER(TRIM(prov)) = 'OP'
        ORDER BY fecha NULLS LAST, concepto
        """
    ) or []

    # Imputaciones por linea (cada retiro imputado a esa linea, con su id para
    # poder DESHACER). Tabla PC-only: puede no existir hasta correr la migracion
    # 0109 -> defensivo (sin imputaciones).
    alloc_rows: dict[str, list[dict]] = {}
    try:
        for a in (db.fetch_all(
            "SELECT id_op_retiro_linea, line_key, fecha, monto "
            "FROM scintela.op_retiro_linea ORDER BY fecha, id_op_retiro_linea"
        ) or []):
            alloc_rows.setdefault(a["line_key"], []).append({
                "id": a["id_op_retiro_linea"],
                "fecha": a.get("fecha"),
                "monto": round(float(a.get("monto") or 0), 2),
            })
    except Exception:  # noqa: BLE001
        alloc_rows = {}

    out: list[dict] = []
    for r in rows:
        credito = round(float(r.get("credito") or 0), 2)
        imps = alloc_rows.get(r.get("line_key"), [])
        retirado = round(sum(i["monto"] for i in imps), 2)
        out.append({
            "line_key": r.get("line_key"),
            "origen": r.get("origen"),
            "fecha": r.get("fecha"),
            "concepto": r.get("concepto") or "",
            "credito": credito,
            "retirado": retirado,
            "restante": round(credito - retirado, 2),
            "imputaciones": imps,
        })
    return out


def crear_op(*, monto: float, de: str = "OP", fecha: date | None = None,
             concepto: str | None = None, usuario: str = "web",
             line_key: str | None = None, line_concepto: str | None = None) -> dict:
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
        # Imputación a la línea OP concreta (display, NO balance): registra
        # a qué crédito OP se descontó este retiro para mostrar el saldo
        # restante por línea. Tabla PC-only (sobrevive el sync). Si todavía
        # no existe (migración 0109 sin correr) se saltea: el retiro igual
        # queda registrado y pega el balance una sola vez.
        if line_key:
            _reg = db.fetch_one(
                "SELECT to_regclass('scintela.op_retiro_linea') AS t", conn=conn
            ) or {}
            if _reg.get("t"):
                db.execute(
                    """
                    INSERT INTO scintela.op_retiro_linea
                        (line_key, fecha, monto, id_retiro, concepto, usuario_crea)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (line_key[:200], fecha, monto, id_retiro,
                     (line_concepto or "")[:120], USUARIO_RETIRO_OP),
                    conn=conn,
                )
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



def imputaciones_de_linea(line_key: str) -> list[dict]:
    """Retiros OP imputados a una linea (line_key), con su id para deshacer.
    Tabla PC-only: puede no existir hasta correr la migracion 0109 -> [] defensivo."""
    try:
        rows = db.fetch_all(
            "SELECT id_op_retiro_linea AS id, fecha, monto "
            "FROM scintela.op_retiro_linea WHERE line_key = %s "
            "ORDER BY fecha, id_op_retiro_linea",
            (line_key,),
        ) or []
    except Exception:  # noqa: BLE001
        return []
    return [{"id": r["id"], "fecha": r.get("fecha"),
             "monto": round(float(r.get("monto") or 0), 2)} for r in rows]



def deshacer_op(id_op_retiro_linea: int, usuario: str = "web") -> dict:
    """Deshace un retiro OP imputado a una linea.

    Borra el retiro de scintela.retiros (REVIERTE el balance, una sola vez) +
    borra la imputacion de scintela.op_retiro_linea (la linea vuelve a SUBIR su
    restante). Auditado con un mov_doble de reverso. Atomico.
    """
    with db.tx() as conn:
        row = db.fetch_one(
            "SELECT id_op_retiro_linea, line_key, monto, id_retiro, fecha, concepto "
            "FROM scintela.op_retiro_linea WHERE id_op_retiro_linea = %s",
            (id_op_retiro_linea,), conn=conn,
        )
        if not row:
            raise ValueError("No encuentro esa imputacion de retiro OP.")
        monto = round(float(row.get("monto") or 0), 2)
        id_retiro = row.get("id_retiro")
        line_key = row.get("line_key")
        # Borrar el retiro (revierte el balance). Robusto al Sync dBase, que
        # REASIGNA id_retiro a los retiros pc-retiro-op: primero validamos que
        # el id siga apuntando a un retiro OP del mismo monto; si no, buscamos
        # por (de=OP, ret, fecha, usuario_crea) para NO borrar otro retiro.
        target_id = None
        if id_retiro:
            _ok = db.fetch_one(
                "SELECT id_retiro FROM scintela.retiros "
                "WHERE id_retiro = %s AND UPPER(TRIM(de)) = 'OP' "
                "AND ROUND(ret, 2) = %s",
                (id_retiro, monto), conn=conn,
            )
            target_id = _ok.get("id_retiro") if _ok else None
        if not target_id:
            _alt = db.fetch_one(
                "SELECT id_retiro FROM scintela.retiros "
                "WHERE UPPER(TRIM(de)) = 'OP' AND ROUND(ret, 2) = %s "
                "AND fecha = %s AND usuario_crea = %s "
                "ORDER BY id_retiro DESC LIMIT 1",
                (monto, row.get("fecha"), USUARIO_RETIRO_OP), conn=conn,
            )
            target_id = _alt.get("id_retiro") if _alt else None
        if target_id:
            db.execute(
                "DELETE FROM scintela.retiros WHERE id_retiro = %s",
                (target_id,), conn=conn,
            )
        # Borrar la imputacion (restaura el restante de la linea).
        db.execute(
            "DELETE FROM scintela.op_retiro_linea WHERE id_op_retiro_linea = %s",
            (id_op_retiro_linea,), conn=conn,
        )
        try:
            import mov_doble as _md
            _md.registrar(
                conn=conn,
                tipo="reverso_retiro_op",
                origen_table="retiros",
                origen_id=int(id_retiro or 0),
                destino_table="retiros",
                destino_id=int(id_retiro or 0),
                importe=monto,
                fecha=row.get("fecha") or today_ec(),
                concepto=f"Reverso retiro OP (linea {line_key}) $ {monto:.2f}"[:200],
                usuario=usuario,
                metadata={"origen": "deshacer_retiro_op", "line_key": line_key},
            )
        except Exception:
            raise
    return {"monto": monto, "line_key": line_key}


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
