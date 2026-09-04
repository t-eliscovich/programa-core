"""Queries de retiros del dueño (scintela.retiros)."""
from datetime import date, timedelta

import db
from filters import today_ec

# Marcador de origen PC para los retiros que NO viven en el dBase (caso
# "retiro OP banco USA"). El sync (import_dbf) preserva estas filas al
# re-cargar RETIROS.DBF en vez de pisarlas. TMT 2026-06-26.
USUARIO_RETIRO_OP = "pc-retiro-op"


def saldo_op() -> dict:
    """Crédito OP que TODAVÍA está en posdatados.

    TMT 2026-09-04 (Tamara: "dale" a leerlo de posdat). Hasta hoy sumaba las
    COMPRAS OP (`scintela.compra`, codigo_prov='OP') menos los retiros desde la
    primera compra viva. Eso era del dBase: las 14 compras OP que quedan son de
    may–jul/2026, once se consumieron por el dBase (sin `op_retiro_linea`) y
    desde que el programa manda el crédito OP se carga A MANO en posdat
    (serie 1003xx, sin compra atrás). Sumar compras daba un "disponible" de
    −220.205 que no medía nada (health op_cierra del 03/09).

    El crédito que vale es el que entra al balance por TOTP: las filas
    posdat prov='OP', banc=0, no anuladas. `crear_op` las ENCOGE con cada
    retiro (`importe += monto`), así que su suma ES lo que falta retirar.

    Devuelve POSITIVO legible:
      credito  = |Σ posdat OP vivos|   (lo que falta retirar)
      n_lineas = cuántas filas lo forman
    """
    r = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS s, COUNT(*) AS n
          FROM scintela.posdat
         WHERE UPPER(TRIM(prov)) = 'OP'
           AND COALESCE(banc, 0) = 0
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or {"s": 0, "n": 0}
    return {
        "credito": round(-float(r.get("s") or 0), 2),
        "n_lineas": int(r.get("n") or 0),
    }


def _posdat_de_line_key(line_key: str, conn=None):
    """Fila posdat OP identificada por line_key 'P|num|concepto' (o None)."""
    if not line_key or not line_key.startswith("P|"):
        return None
    try:
        _, num_s, concepto = line_key.split("|", 2)
        num = int(num_s or 0)
    except ValueError:
        return None
    return db.fetch_one(
        "SELECT id_posdat, importe FROM scintela.posdat "
        "WHERE UPPER(TRIM(prov)) = 'OP' AND COALESCE(num, 0) = %s "
        "AND COALESCE(concepto, '') = %s ORDER BY id_posdat LIMIT 1",
        (num, concepto), conn=conn,
    )


def _col_bajo_posdat(conn=None) -> bool:
    """True si la columna op_retiro_linea.bajo_posdat existe (mig 0111)."""
    try:
        r = db.fetch_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_schema = 'scintela' AND table_name = 'op_retiro_linea' "
            "AND column_name = 'bajo_posdat'",
            conn=conn,
        )
        return bool(r)
    except Exception:  # noqa: BLE001
        return False


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
    # TMT 2026-07-20 (duena): "para hacer un aporte de capital usualmente
    # hacia un retiro en negativo" (paridad dBase). NEGATIVO = APORTE del
    # accionista: dividendos (URET) baja y, si va imputado a una linea OP,
    # el credito OP crece (importe += monto negativo, espejo exacto del
    # retiro). deshacer_op ya es agnostico al signo. Solo se bloquea el 0.
    if not monto:
        raise ValueError("El monto no puede ser cero.")
    de = (de or "OP").strip().upper()[:5] or "OP"
    fecha = fecha or today_ec()
    if not concepto:
        base = "APORTE" if monto < 0 else "RR"
        concepto = f"{base} {de} banco USA"
    concepto = concepto[:100]

    with db.tx() as conn:
        # TMT 2026-07-06 v5 (dueña: "el retiro es SIEMPRE positivo" pero
        # "utilidad no debería cambiar"): el retiro se guarda POSITIVO (como
        # los 206 RR OP del dBase) → Dividendos lo muestra sumando. La
        # neutralidad de la utilidad se logra en la FÓRMULA (informes):
        # uret_mes_ajustado() cuenta los pc-retiro-op en NEGATIVO dentro del
        # cálculo (totl/usuti), porque su plata ya restó por el lado del
        # posdat OP. Pasivos RESTA, dividendos SUMA, utilidad QUIETA.
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
                # TMT 2026-07-06 v6 DEFINITIVO (dueña, tras 5 iteraciones):
                # el crédito OP se CONSUME — importe += monto (−115.207 →
                # −63.841, "es un pasivo negativo, saco retiros, se hace
                # menos negativo"). Consecuencia aceptada: el TOTAL de
                # Pasivos SUBE el monto (menos crédito descontando). La
                # utilidad queda QUIETA sola: el retiro POSITIVO en
                # dividendos (URET, que vive dentro del Total Activo)
                # compensa exactamente la suba de pasivos. Sin ajustes
                # especiales en informes. posdat es PC-owned (sync no pisa).
                # Requiere mig 0111 (bajo_posdat); sin la columna cae al
                # comportamiento viejo (display-only) para no romper.
                _con_col = _col_bajo_posdat(conn=conn)
                _bajo = False
                if _con_col:
                    _pd = _posdat_de_line_key(line_key, conn=conn)
                    if _pd:
                        db.execute(
                            "UPDATE scintela.posdat "
                            "SET importe = ROUND(COALESCE(importe, 0) + %s, 2) "
                            "WHERE id_posdat = %s",
                            (monto, _pd["id_posdat"]), conn=conn,
                        )
                        _bajo = True
                if _con_col:
                    db.execute(
                        """
                        INSERT INTO scintela.op_retiro_linea
                            (line_key, fecha, monto, id_retiro, concepto,
                             usuario_crea, bajo_posdat)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (line_key[:200], fecha, monto, id_retiro,
                         (line_concepto or "")[:120], USUARIO_RETIRO_OP, _bajo),
                        conn=conn,
                    )
                else:
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
            "SELECT id_op_retiro_linea AS id, fecha, monto, bajo_posdat "
            "FROM scintela.op_retiro_linea WHERE line_key = %s "
            "ORDER BY fecha, id_op_retiro_linea",
            (line_key,),
        ) or []
    except Exception:  # noqa: BLE001
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
             "monto": round(float(r.get("monto") or 0), 2),
             "bajo_posdat": bool(r.get("bajo_posdat"))} for r in rows]



def deshacer_op(id_op_retiro_linea: int, usuario: str = "web") -> dict:
    """Deshace un retiro OP imputado a una linea.

    Borra el retiro de scintela.retiros (REVIERTE el balance, una sola vez) +
    borra la imputacion de scintela.op_retiro_linea (la linea vuelve a SUBIR su
    restante). Auditado con un mov_doble de reverso. Atomico.
    """
    with db.tx() as conn:
        _con_col = _col_bajo_posdat(conn=conn)
        _cols = ("id_op_retiro_linea, line_key, monto, id_retiro, fecha, concepto"
                 + (", bajo_posdat" if _con_col else ""))
        row = db.fetch_one(
            f"SELECT {_cols} "
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
        # TMT 2026-07-06 v6: el retiro CONSUMIÓ crédito (importe += monto) —
        # el deshacer lo devuelve (importe -= monto, la fila vuelve a bajar).
        # Las imputaciones viejas display-only no tocan posdat.
        if row.get("bajo_posdat"):
            _pd = _posdat_de_line_key(line_key, conn=conn)
            if not _pd:
                raise ValueError(
                    "No encuentro la fila posdat OP de esta imputacion para "
                    "devolverle el monto — no deshago nada (¿se borró o cambió "
                    "el concepto de la fila OP?)."
                )
            db.execute(
                "UPDATE scintela.posdat "
                "SET importe = ROUND(COALESCE(importe, 0) - %s, 2) "
                "WHERE id_posdat = %s",
                (monto, _pd["id_posdat"]), conn=conn,
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
    id_retiro: int | None = None,
) -> list[dict]:
    """Histórico de retiros filtrable por concepto/de + fecha + banco.

    TMT 2026-08-07 (dueña, sobre los links del historial): *"si al clickear hay
    que buscar la fila a ojo, el link no está terminado"*. `id_retiro` deja UNA
    fila — la que menciona el movimiento — y apaga los demás filtros: el
    concepto buscado, el código `de` y el rango de fechas se refieren a lo que
    se estaba mirando ANTES, no a la fila que se pidió, y cualquiera de los
    tres la escondería.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto, r.clave,
               r.id_transaccion_bancaria,
               COALESCE(b.nombre, '') AS banco
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE (%(id_retiro)s IS NULL OR r.id_retiro = %(id_retiro)s)
          AND (%(id_retiro)s IS NOT NULL
               OR %(q)s IS NULL
               OR UPPER(COALESCE(r.concepto,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(r.de,'')) LIKE UPPER(%(like)s))
          AND (%(id_retiro)s IS NOT NULL
               OR %(de)s IS NULL OR UPPER(r.de) = UPPER(%(de)s))
          AND (%(id_retiro)s IS NOT NULL
               OR %(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
          AND (%(id_retiro)s IS NOT NULL
               OR %(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
        ORDER BY r.fecha DESC, r.id_retiro DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like, "de": de or None,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
            "id_retiro": int(id_retiro) if id_retiro else None,
        },
    ) or []


def totales_por_persona(desde: str | None = None, hasta: str | None = None,
                        id_retiro: int | None = None) -> list[dict]:
    """Cuánto retiró cada socio en el periodo. Útil para informe trimestral.

    TMT 2026-08-07: `id_retiro` también acá. Alimenta los chips «Por código»
    que van ARRIBA de la grilla; sin pasárselo, pedir un retiro por id mostraba
    los totales del último año sobre una sola fila (el bug del hero
    incongruente que ya se pagó en /posdat).
    """
    # OJO: la ventana de 365 días es un DEFAULT de esta función, no algo que
    # mande la URL — y un retiro de hace dos años queda afuera. Pidiendo por id
    # no se aplica ninguna ventana.
    id_retiro = int(id_retiro) if id_retiro else None
    desde_d = None if id_retiro else (desde or (today_ec() - timedelta(days=365)).isoformat())
    hasta_d = None if id_retiro else (hasta or today_ec().isoformat())
    return db.fetch_all(
        """
        SELECT COALESCE(de, '(sin asignar)') AS de,
               SUM(ret)                       AS total,
               COUNT(*)                       AS n_retiros,
               MAX(fecha)                     AS ultimo
        FROM scintela.retiros
        WHERE (%(id_retiro)s IS NULL OR id_retiro = %(id_retiro)s)
          AND (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
        GROUP BY 1
        ORDER BY total DESC
        """,
        {"id_retiro": id_retiro, "desde": desde_d, "hasta": hasta_d},
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


def resumen(desde: str | None = None, hasta: str | None = None,
            id_retiro: int | None = None) -> dict:
    """Total + n del filtro actual.

    TMT 2026-08-07: pedir UN retiro por id apaga la ventana de 90 días.
    Es el filtro que muerde de verdad en esta pantalla: `buscar()` no tiene
    default de fechas, pero el hero sí — un retiro de hace 4 meses mostraba
    "Total retirado" de los últimos 90 días (que NO lo incluyen) arriba de la
    fila pedida. El "Periodo" pasa a ser la fecha de esa misma fila.
    """
    id_retiro = int(id_retiro) if id_retiro else None
    desde_d = None if id_retiro else (desde or (today_ec() - timedelta(days=90)).isoformat())
    hasta_d = None if id_retiro else (hasta or today_ec().isoformat())
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0)        AS total,
               COUNT(*)                      AS n,
               COUNT(DISTINCT de)            AS n_personas,
               MIN(fecha)                    AS f_min,
               MAX(fecha)                    AS f_max
        FROM scintela.retiros
        WHERE (%(id_retiro)s IS NULL OR id_retiro = %(id_retiro)s)
          AND (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
        """,
        {"id_retiro": id_retiro, "desde": desde_d, "hasta": hasta_d},
    ) or {}
    n = int(row.get("n") or 0)
    total = float(row.get("total") or 0)
    if id_retiro:
        # Sin ventana no hay "desde/hasta" que mostrar: el periodo del KPI es
        # el día del retiro pedido (o '—' si el id no existe).
        desde_d = row.get("f_min")
        hasta_d = row.get("f_max")
    return {
        "n":               n,
        "n_personas":      int(row.get("n_personas") or 0),
        "total":           total,
        "ticket_promedio": (total / n) if n else 0.0,
        "desde":           desde_d,
        "hasta":           hasta_d,
    }
