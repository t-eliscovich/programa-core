"""Provisiones — acumulaciones recurrentes (alquileres, servicios, etc.).

scintela.provisiones: id_provisiones, concepto, importe, periodo_aplica,
   fecha_crea, fecha_actualiza, usuario_crea, usuario_actualiza
   (OJO: usa fecha_actualiza/usuario_actualiza, NO fecha_modifica — schema distinto).
"""
import db


def por_id(id_provisiones: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_provisiones, concepto, importe, periodo_aplica,
               fecha_crea, fecha_actualiza, usuario_crea, usuario_actualiza
        FROM scintela.provisiones
        WHERE id_provisiones = %s
        """,
        (id_provisiones,),
    )


def crear(*, concepto: str, importe, periodo_aplica: str, usuario: str = "web") -> dict:
    """Reglas:
        - concepto      varchar(50)  — truncado a 50
        - periodo_aplica varchar(30) — truncado a 30
        - usuario_crea  varchar(20)  — truncado a 20
    Schema verificado en docs/SCHEMA.txt.
    """
    if not concepto:
        raise ValueError("Concepto requerido.")
    if importe is None:
        raise ValueError("Importe requerido.")
    if not periodo_aplica:
        raise ValueError("Período requerido.")
    return db.execute_returning(
        """
        INSERT INTO scintela.provisiones
            (concepto, importe, periodo_aplica, fecha_crea, usuario_crea)
        VALUES (%s, %s, %s, CURRENT_DATE, %s)
        RETURNING id_provisiones
        """,
        (concepto[:50], importe, periodo_aplica[:30], usuario[:20]),
    ) or {}


def editar(
    id_provisiones: int,
    *,
    concepto: str | None = None,
    importe=None,
    periodo_aplica: str | None = None,
    usuario: str = "web",
) -> int:
    campos = []
    params: list = []
    if concepto is not None:
        campos.append("concepto = %s")
        params.append(concepto[:50])
    if importe is not None:
        campos.append("importe = %s")
        params.append(importe)
    if periodo_aplica is not None:
        campos.append("periodo_aplica = %s")
        params.append(periodo_aplica[:30])
    if not campos:
        return 0
    campos.append("fecha_actualiza = CURRENT_DATE")
    campos.append("usuario_actualiza = %s")
    # usuario_actualiza es varchar(30) en el schema
    params.append(usuario[:30])
    params.append(id_provisiones)
    return db.execute(
        f"UPDATE scintela.provisiones SET {', '.join(campos)} WHERE id_provisiones = %s",
        tuple(params),
    )


def eliminar(id_provisiones: int) -> int:
    return db.execute(
        "DELETE FROM scintela.provisiones WHERE id_provisiones = %s",
        (id_provisiones,),
    )


def lista(q: str = "") -> list[dict]:
    """Listar provisiones con búsqueda opcional por concepto/período."""
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT id_provisiones, concepto, importe, periodo_aplica,
               fecha_crea, fecha_actualiza,
               usuario_crea, usuario_actualiza
        FROM scintela.provisiones
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(concepto, '')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(periodo_aplica, '')) LIKE UPPER(%(like)s))
        ORDER BY concepto
        """,
        {"q": q or None, "like": like},
    )


def resumen() -> dict:
    """Resumen completo: total general + total mensual estimado.

    El cálculo de "mensual estimado" prorratea los periodos: una provisión
    bimestral se cuenta como /2, trimestral /3, semestral /6, anual /12.
    Si el periodo no matchea, asume mensual (la lectura literal del campo
    no siempre está estandarizada).

    NOTA crítica: los `%` literales del LIKE están escapados como `%%`
    porque psycopg2 los confunde con placeholders cuando params no es None
    (db.fetch_one pasa `()` por default en lugar de None) — sin escapar
    da "tuple index out of range".
    """
    return db.fetch_one(
        """
        SELECT COUNT(*) AS n_total,
               COALESCE(SUM(importe), 0) AS total_general,
               COUNT(*) FILTER (WHERE UPPER(COALESCE(periodo_aplica,'')) LIKE '%%MENSUAL%%')   AS n_mensual,
               COALESCE(SUM(CASE
                   WHEN UPPER(COALESCE(periodo_aplica,'')) LIKE '%%ANUAL%%'      THEN importe / 12.0
                   WHEN UPPER(COALESCE(periodo_aplica,'')) LIKE '%%SEMESTR%%'    THEN importe / 6.0
                   WHEN UPPER(COALESCE(periodo_aplica,'')) LIKE '%%TRIMEST%%'    THEN importe / 3.0
                   WHEN UPPER(COALESCE(periodo_aplica,'')) LIKE '%%BIMESTR%%'    THEN importe / 2.0
                   ELSE importe
               END), 0) AS prorrateado_mensual
        FROM scintela.provisiones
        """,
    ) or {
        "n_total": 0, "total_general": 0,
        "n_mensual": 0, "prorrateado_mensual": 0,
    }


# --- Backwards compat: total_mensual() viejo nombre, ahora wrapper de resumen()
def total_mensual() -> dict | None:
    """DEPRECATED — usar resumen(). Devuelve {total, n} mensuales puros.

    `%` escapados como `%%` por la misma razón que en `resumen()`.
    """
    return db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total, COUNT(*) AS n
        FROM scintela.provisiones
        WHERE UPPER(COALESCE(periodo_aplica, '')) LIKE '%%MENSUAL%%'
        """,
    )
