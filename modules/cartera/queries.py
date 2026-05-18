"""Queries de cartera — aging buckets + stop automático.

Usa `COALESCE(vencimiento, fecha)` en lugar de `vencimiento` solo porque el
dump tiene un puñado de filas históricas con NULL en vencimiento; se las
ubica en algún bucket en lugar de perderlas del reporte.

Filtro canónico de "factura viva":
    COALESCE(saldo, 0) > 0
    AND (stat IS NULL OR stat IN ('Z','A','',' '))

Es el mismo que usan `dashboard.queries.kpis_dueno`, `informes.queries.totf`
y `clientes.queries.buscar`. No divergir.
"""
from datetime import date

import db

# Stats de cheque que cuentan como "en cartera" (vivos, no efectivos todavía).
# Restan de la cartera por cliente — el cliente nos los dio pero no se cobraron.
# Los anticipos negativos (CONCEPTO=9999 espejo) ya tienen importe negativo,
# así que SUM contabiliza el signo correcto.
CHEQUES_EN_CARTERA_STATS = ("Z", "1", "2", "3", "P", "D", "A")


def aging_buckets() -> list[dict]:
    """Saldos por cliente con descomposición en cuatro buckets de mora.

    Paridad dBase (addendum batch 22 §E): la cartera por cliente es
    `factura.saldo` MENOS `cheque.importe` para cheques en cartera (Z, 1, 2,
    3, P, D, A). El cliente ya nos dio esos cheques aunque no se cobraron.
    Anticipos (CONCEPTO=9999 espejo, importe negativo) restan automáticamente
    via SUM.

    Orden: peor primero (90+ descendente, luego saldo total).

    TMT 2026-05-18: aplica la misma allocation que aging_totales para que
    la suma de buckets de cada fila coincida con su saldo_total
    (saldo_facturas − cheques_en_cartera). Los cheques se asignan a los
    buckets más jóvenes primero.
    """
    rows = db.fetch_all(
        """
        WITH cheques_cli AS (
            SELECT codigo_cli,
                   COALESCE(SUM(importe), 0) AS en_cartera
            FROM scintela.cheque
            WHERE stat IN ('Z','1','2','3','P','D','A')
              AND codigo_cli IS NOT NULL
            GROUP BY codigo_cli
        )
        SELECT f.codigo_cli,
               COALESCE(c.nombre, '(sin nombre)')  AS nombre,
               COALESCE(c.stop, 'N')               AS stop,
               COALESCE(c.cupo, 0)                 AS cupo,
               COALESCE(c.vend, '')                AS vend,
               COALESCE(c.telefono, '')            AS telefono,
               COALESCE(c.correo, '')              AS correo,
               COUNT(*)                            AS n_facturas,
               COALESCE(SUM(f.saldo), 0)           AS saldo_facturas,
               COALESCE(MAX(cc.en_cartera), 0)     AS cheques_en_cartera,
               COALESCE(SUM(f.saldo), 0)
                  - COALESCE(MAX(cc.en_cartera), 0) AS saldo_total,
               COALESCE(SUM(CASE
                   WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) <= 30
                   THEN f.saldo ELSE 0 END), 0)    AS b0_30,
               COALESCE(SUM(CASE
                   WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) BETWEEN 31 AND 60
                   THEN f.saldo ELSE 0 END), 0)    AS b31_60,
               COALESCE(SUM(CASE
                   WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) BETWEEN 61 AND 90
                   THEN f.saldo ELSE 0 END), 0)    AS b61_90,
               COALESCE(SUM(CASE
                   WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) > 90
                   THEN f.saldo ELSE 0 END), 0)    AS b90_plus,
               MIN(COALESCE(f.vencimiento, f.fecha)) AS vence_mas_viejo,
               MAX(CURRENT_DATE - COALESCE(f.vencimiento, f.fecha)) AS dias_mora_max
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c     ON c.codigo_cli = f.codigo_cli
        LEFT JOIN cheques_cli cc         ON cc.codigo_cli = f.codigo_cli
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
        GROUP BY f.codigo_cli, c.nombre, c.stop, c.cupo, c.vend, c.telefono, c.correo
        ORDER BY b90_plus DESC, saldo_total DESC
        """
    ) or []

    # Asignar cheques_en_cartera por cliente contra sus buckets (jóvenes primero)
    for r in rows:
        pendiente = float(r.get("cheques_en_cartera") or 0)
        for k in ("b0_30", "b31_60", "b61_90", "b90_plus"):
            actual = float(r.get(k) or 0)
            toma = min(actual, pendiente)
            r[k] = actual - toma
            pendiente -= toma
            if pendiente <= 0:
                break
    return rows


def aging_totales() -> dict:
    """Totales agregados para las 4 tiles del encabezado.

    `total` = factura_saldo - cheques_en_cartera (paridad dBase). Los
    buckets de mora se mantienen sobre factura.saldo solamente — la
    distribución por días es de las facturas, no de los cheques.
    """
    row = db.fetch_one(
        """
        WITH cheques_total AS (
            SELECT COALESCE(SUM(importe), 0) AS en_cartera
              FROM scintela.cheque
             WHERE stat IN ('Z','1','2','3','P','D','A')
               AND codigo_cli IS NOT NULL
        )
        SELECT
            COALESCE(SUM(CASE
                WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) <= 30
                THEN f.saldo ELSE 0 END), 0) AS b0_30,
            COALESCE(SUM(CASE
                WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) BETWEEN 31 AND 60
                THEN f.saldo ELSE 0 END), 0) AS b31_60,
            COALESCE(SUM(CASE
                WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) BETWEEN 61 AND 90
                THEN f.saldo ELSE 0 END), 0) AS b61_90,
            COALESCE(SUM(CASE
                WHEN CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) > 90
                THEN f.saldo ELSE 0 END), 0) AS b90_plus,
            COALESCE(SUM(f.saldo), 0) AS saldo_facturas,
            (SELECT en_cartera FROM cheques_total) AS cheques_en_cartera,
            COALESCE(SUM(f.saldo), 0)
                - (SELECT en_cartera FROM cheques_total)  AS total,
            COUNT(*)                        AS n_facturas,
            COUNT(DISTINCT f.codigo_cli)    AS n_clientes
        FROM scintela.factura f
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
        """
    )
    if not row:
        return {
            "b0_30": 0.0, "b31_60": 0.0, "b61_90": 0.0, "b90_plus": 0.0,
            "total": 0.0, "saldo_facturas": 0.0, "cheques_en_cartera": 0.0,
            "n_facturas": 0, "n_clientes": 0,
        }

    # TMT 2026-05-18 — Bug "0-30 días > total": los buckets sumaban
    # f.saldo bruto, pero `total = saldo_facturas - cheques_en_cartera`.
    # Quedaba mathematically inconsistent: sum(buckets) = saldo_facturas
    # > total. Asignamos los cheques en cartera contra los buckets desde
    # el más joven (los cheques posdatados típicamente cancelan facturas
    # recientes). Con esto sum(buckets) == total siempre.
    b0_30    = float(row["b0_30"] or 0)
    b31_60   = float(row["b31_60"] or 0)
    b61_90   = float(row["b61_90"] or 0)
    b90_plus = float(row["b90_plus"] or 0)
    pendiente = float(row.get("cheques_en_cartera") or 0)
    for label in ("b0_30", "b31_60", "b61_90", "b90_plus"):
        actual = {"b0_30": b0_30, "b31_60": b31_60,
                  "b61_90": b61_90, "b90_plus": b90_plus}[label]
        toma = min(actual, pendiente)
        if label == "b0_30":    b0_30    -= toma
        if label == "b31_60":   b31_60   -= toma
        if label == "b61_90":   b61_90   -= toma
        if label == "b90_plus": b90_plus -= toma
        pendiente -= toma
        if pendiente <= 0:
            break

    return {
        "b0_30":              b0_30,
        "b31_60":             b31_60,
        "b61_90":             b61_90,
        "b90_plus":           b90_plus,
        "total":              float(row["total"] or 0),
        "saldo_facturas":     float(row.get("saldo_facturas") or 0),
        "cheques_en_cartera": float(row.get("cheques_en_cartera") or 0),
        "n_facturas":         int(row["n_facturas"] or 0),
        "n_clientes":         int(row["n_clientes"] or 0),
    }


def clientes_con_vencido(umbral_dias: int = 90) -> list[dict]:
    """Clientes con facturas vencidas más de N días que NO están ya en stop.

    Un SELECT DISTINCT — la lista previa de quién pasaría a stop, para
    previsualizar antes de aplicar la acción.
    """
    return db.fetch_all(
        """
        SELECT c.codigo_cli,
               c.nombre,
               SUM(f.saldo)                           AS saldo_vencido,
               COUNT(*)                               AS n_facturas,
               MAX(CURRENT_DATE - COALESCE(f.vencimiento, f.fecha)) AS dias_mora_max
        FROM scintela.factura f
        JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
          AND CURRENT_DATE - COALESCE(f.vencimiento, f.fecha) > %s
          AND COALESCE(c.stop, 'N') != 'S'
        GROUP BY c.codigo_cli, c.nombre
        ORDER BY saldo_vencido DESC
        """,
        (umbral_dias,),
    )


def aging_por_grupo() -> list[dict]:
    """Cartera viva agrupada por `grupo_cliente.codigo_padre`.

    Suma los saldos de todos los `codigo_hijo` que apuntan a un mismo
    `codigo_padre`. Clientes que no están en ningún grupo aparecen como
    su propio "grupo" (codigo_padre = codigo_cli).

    Reemplaza la PROCEDURE GRUPOS de INFORMES.PRG: en el legacy se
    reasignaba el campo `cliente.cliente` (texto) a un código de agrupador;
    acá usamos la tabla relacional `scintela.grupo_cliente`.
    """
    return db.fetch_all(
        """
        WITH base AS (
            SELECT f.codigo_cli,
                   COALESCE(g.codigo_padre, f.codigo_cli) AS codigo_grupo,
                   COALESCE(SUM(f.saldo), 0) AS saldo
            FROM scintela.factura f
            LEFT JOIN scintela.grupo_cliente g ON g.codigo_hijo = f.codigo_cli
            WHERE COALESCE(f.saldo, 0) > 0
              AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
            GROUP BY f.codigo_cli, COALESCE(g.codigo_padre, f.codigo_cli)
        )
        SELECT b.codigo_grupo,
               COALESCE(c.nombre, b.codigo_grupo)        AS nombre_padre,
               COUNT(DISTINCT b.codigo_cli)              AS n_hijos,
               STRING_AGG(b.codigo_cli, ', '
                          ORDER BY b.codigo_cli)         AS hijos,
               SUM(b.saldo)                              AS saldo_total
        FROM base b
        LEFT JOIN scintela.cliente c ON c.codigo_cli = b.codigo_grupo
        GROUP BY b.codigo_grupo, c.nombre
        HAVING SUM(b.saldo) > 0
        ORDER BY SUM(b.saldo) DESC
        """
    ) or []


def tomar_snapshot(fecha: date | None = None) -> dict:
    """ITEM #4 — Toma snapshot diario de la cartera por cliente.

    Reemplazo del legacy `F:\\LUNES\\FACTURAS`, etc. (MENU.PRG L1582-1660
    PROCEDURE CONTROLC): el dBase copiaba la base entera a una carpeta por
    día de la semana; acá guardamos por cliente saldo agregado + n_facturas
    en `scintela.cartera_snapshots`.

    Idempotente: ON CONFLICT (fecha, codigo_cli) DO UPDATE — si se corre
    dos veces el mismo día, sobreescribe.

    Cuenta solamente facturas vivas (`saldo > 0 AND stat IN ('Z','A','',' ')`).
    NO descuenta cheques en cartera por cliente (eso lo hace
    `aging_buckets()` para mostrar al usuario; el snapshot es la cartera
    "bruta" de facturas, espejo de lo que dBase guardaba).

    Devuelve `{fecha, n_clientes, n_filas_insertadas, n_filas_actualizadas,
    saldo_total}`.
    """
    fecha = fecha or date.today()
    n_ins = 0
    n_upd = 0
    saldo_total = 0.0
    # TMT 2026-05-15 (re-audit C4): SELECT + INSERTs en la MISMA tx con
    # advisory lock para serializar dos snapshots del mismo día. Sin
    # esto el SELECT inicial corría en autocommit y un cobro posteado
    # entre el SELECT y el INSERT contaminaba el snapshot.
    with db.tx() as conn:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('cartera_snapshot'))",
            conn=conn,
        )
        rows = db.fetch_all(
            """
            SELECT f.codigo_cli,
                   COALESCE(SUM(f.saldo), 0) AS saldo_total,
                   COUNT(*)                  AS n_facturas
            FROM scintela.factura f
            WHERE COALESCE(f.saldo, 0) > 0
              AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
            GROUP BY f.codigo_cli
            """,
            conn=conn,
        ) or []

        if not rows:
            return {"fecha": fecha.isoformat(), "n_clientes": 0,
                    "n_filas_insertadas": 0, "n_filas_actualizadas": 0,
                    "saldo_total": 0.0}

        for r in rows:
            saldo_total += float(r.get("saldo_total") or 0)
            res = db.execute_returning(
                """
                INSERT INTO scintela.cartera_snapshots
                    (fecha, codigo_cli, saldo_total, n_facturas)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (fecha, codigo_cli) DO UPDATE
                   SET saldo_total = EXCLUDED.saldo_total,
                       n_facturas  = EXCLUDED.n_facturas,
                       snapshot_ts = CURRENT_TIMESTAMP
                RETURNING (xmax = 0) AS inserted
                """,
                (fecha, r["codigo_cli"], r["saldo_total"], r["n_facturas"]),
                conn=conn,
            )
            if res and res.get("inserted"):
                n_ins += 1
            else:
                n_upd += 1
    return {
        "fecha": fecha.isoformat(),
        "n_clientes": len(rows),
        "n_filas_insertadas": n_ins,
        "n_filas_actualizadas": n_upd,
        "saldo_total": saldo_total,
    }


def comparar_contra_snapshot(fecha_snapshot=None) -> dict:
    """ITEM #4 — Compara cartera de hoy contra un snapshot anterior.

    Replica de PROCEDURE CONTROLC (MENU.PRG L1582-1660). Devuelve por
    cliente: saldo_hoy, saldo_snapshot, diferencia (positivo=aumento de
    deuda, negativo=cobrado), n_facturas_hoy, n_facturas_snapshot, delta_n.

    `fecha_snapshot=None` toma el snapshot más reciente disponible
    (excluyendo hoy si hay snapshot de hoy → toma el anterior).

    Si no hay snapshots todavía, devuelve `{filas: [], fecha_snapshot: None,
    error: '...'}` con un mensaje informativo.
    """
    if fecha_snapshot:
        if isinstance(fecha_snapshot, str):
            from datetime import datetime as _dt
            try:
                fecha_snapshot = _dt.fromisoformat(fecha_snapshot).date()
            except (ValueError, TypeError):
                return {"filas": [], "fecha_snapshot": None,
                        "error": "Fecha de snapshot inválida."}
    else:
        # Snapshot más reciente que NO sea de hoy.
        # TMT 2026-05-15 (re-audit H1): si sólo hay un snapshot de hoy NO
        # caemos silenciosamente a comparar "hoy vs hoy" — eso muestra
        # diferencia=0 en todas las filas y le miente al usuario diciéndole
        # que la cartera está estable. Mejor devolvemos error explícito.
        r = db.fetch_one(
            """
            SELECT MAX(fecha) AS fecha
              FROM scintela.cartera_snapshots
             WHERE fecha < CURRENT_DATE
            """
        )
        fecha_snapshot = r.get("fecha") if r else None

    if not fecha_snapshot:
        return {"filas": [], "fecha_snapshot": None, "totales": {},
                "error": "No hay snapshots ANTERIORES a hoy. Corré "
                "scripts/tomar_snapshot_cartera.py al menos un día antes "
                "de comparar (si ya lo corriste hoy, esperá hasta mañana "
                "o pasá ?fecha=YYYY-MM-DD explícita)."}

    filas = db.fetch_all(
        """
        WITH hoy AS (
            SELECT f.codigo_cli,
                   COALESCE(SUM(f.saldo), 0) AS saldo_hoy,
                   COUNT(*)                  AS n_facturas_hoy
              FROM scintela.factura f
             WHERE COALESCE(f.saldo, 0) > 0
               AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
             GROUP BY f.codigo_cli
        ),
        snap AS (
            SELECT codigo_cli,
                   COALESCE(saldo_total, 0) AS saldo_snapshot,
                   COALESCE(n_facturas, 0)  AS n_facturas_snapshot
              FROM scintela.cartera_snapshots
             WHERE fecha = %(fecha_snap)s
        )
        SELECT COALESCE(h.codigo_cli, s.codigo_cli) AS codigo_cli,
               COALESCE(c.nombre, '(sin nombre)')   AS nombre,
               COALESCE(h.saldo_hoy, 0)             AS saldo_hoy,
               COALESCE(s.saldo_snapshot, 0)        AS saldo_snapshot,
               COALESCE(h.saldo_hoy, 0)
                 - COALESCE(s.saldo_snapshot, 0)    AS diferencia,
               COALESCE(h.n_facturas_hoy, 0)        AS n_facturas_hoy,
               COALESCE(s.n_facturas_snapshot, 0)   AS n_facturas_snapshot,
               COALESCE(h.n_facturas_hoy, 0)
                 - COALESCE(s.n_facturas_snapshot, 0) AS delta_n
          FROM hoy h
     FULL OUTER JOIN snap s ON s.codigo_cli = h.codigo_cli
     LEFT JOIN scintela.cliente c
            ON c.codigo_cli = COALESCE(h.codigo_cli, s.codigo_cli)
         ORDER BY ABS(COALESCE(h.saldo_hoy, 0)
                      - COALESCE(s.saldo_snapshot, 0)) DESC,
                  COALESCE(h.saldo_hoy, 0) DESC
        """,
        {"fecha_snap": fecha_snapshot},
    ) or []

    totales = {
        "saldo_hoy":         sum(float(f.get("saldo_hoy") or 0)         for f in filas),
        "saldo_snapshot":    sum(float(f.get("saldo_snapshot") or 0)    for f in filas),
        "diferencia":        sum(float(f.get("diferencia") or 0)        for f in filas),
        "cobrado_o_bajo":    sum(float(f.get("diferencia") or 0)
                                 for f in filas if float(f.get("diferencia") or 0) < 0),
        "aumentado":         sum(float(f.get("diferencia") or 0)
                                 for f in filas if float(f.get("diferencia") or 0) > 0),
        "n_clientes":        len(filas),
        "n_facturas_hoy":    sum(int(f.get("n_facturas_hoy") or 0)      for f in filas),
        "n_facturas_snap":   sum(int(f.get("n_facturas_snapshot") or 0) for f in filas),
    }

    fecha_iso = fecha_snapshot.isoformat() if hasattr(fecha_snapshot, "isoformat") else str(fecha_snapshot)
    return {"filas": filas, "fecha_snapshot": fecha_iso,
            "totales": totales, "error": None}


def snapshots_disponibles(limite: int = 30) -> list:
    """Lista de fechas de snapshot para popular el selector."""
    return [
        r["fecha"]
        for r in (db.fetch_all(
            """
            SELECT fecha
              FROM scintela.cartera_snapshots
             GROUP BY fecha
             ORDER BY fecha DESC
             LIMIT %s
            """,
            (max(1, int(limite)),),
        ) or [])
    ]


# ---------------------------------------------------------------------------
# ITEM #11 — CARTERA × color × cliente (INFORMES.PRG L830-918)
# ---------------------------------------------------------------------------

# Colores legacy del PRG L832 — 14 códigos de 3 chars. Aparecen como
# scintela.tinto.cod en la base actual.
COLORES_LEGACY = ['MAR', 'NEG', 'BLA', 'FRE', 'ELE', 'CEL', 'RAT',
                  'CAR', 'BAN', 'JOS', 'JAS', 'REY', 'ROY', 'ROS']


def cartera_por_cliente_y_color(meses_atras: int = 3) -> dict:
    """ITEM #11 — Cruza cartera viva con piezas tinturadas por color.

    Replica INFORMES.PRG L830-918 ("ESTADISTICA"): el dBase cruzaba la
    tabla PIEZAS (F:\\TINTELA\\PIEZAS) con CARTERA para mostrar, por cada
    cliente con saldo, cuántos kg de cada COLOR/TIPO le mandamos en TOPER,
    JERSEY y PIQUE.

    En PG no tenemos `piezas`. La única tabla con `color` viva es
    `scintela.tinto` (la tabla del tinturado mensual). Hace una
    aproximación distinta: agrupa **kg tinturados** por color por cliente
    aproximando vía facturas en la ventana — porque scintela.tinto NO
    tiene `codigo_cli` (es interna a producción).

    Por eso esto queda flagueado como "necesita decisión humana":
    devuelve dos secciones — una con TOTALES por color (sin desagregar
    por cliente, usando scintela.tinto), y otra con cartera viva por
    cliente (sin colores). El usuario decide si vale o si necesitamos
    portar la tabla PIEZAS legacy.

    Devuelve:
        {
          "filas": [{codigo_cli, nombre, saldo_total, colores: {COD: kg, ...}}],
          "colores_orden": [...],   # 14 códigos 3-char del legacy
          "fuente_color": "scintela.tinto.cod (sin codigo_cli — agregado global)",
          "necesita_decision": True|False,
          "nota_decision": "...",
          "meses_atras": N,
          "fecha_desde": iso,
        }
    """
    from datetime import timedelta as _td

    # TMT 2026-05-15 (re-audit H2): clamp y guardia contra inputs hostiles.
    # Antes int(meses_atras) crasheaba con ValueError; y sin cap superior
    # se podía pasar meses_atras=2**31 → OverflowError en timedelta.
    try:
        meses_atras_int = int(meses_atras or 3)
    except (TypeError, ValueError):
        meses_atras_int = 3
    meses_atras_int = max(1, min(meses_atras_int, 24))
    desde = date.today() - _td(days=meses_atras_int * 31)

    # Cartera viva por cliente (filtro canónico — ver `aging_buckets`).
    clientes = db.fetch_all(
        """
        SELECT f.codigo_cli,
               COALESCE(c.nombre, '(sin nombre)')  AS nombre,
               COALESCE(SUM(f.saldo), 0)           AS saldo_total
          FROM scintela.factura f
          LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE COALESCE(f.saldo, 0) > 0
           AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
         GROUP BY f.codigo_cli, c.nombre
         ORDER BY saldo_total DESC
        """
    ) or []

    # Suma TOTAL por color desde scintela.tinto (no por cliente).
    # tinto.cod = color de 3 letras (mismo set que COLORES_LEGACY).
    # Sumamos toper+jersey+pique como "kg tinturados visibles en facturación"
    # (en dBase eran 3 paneles distintos; acá colapsamos a totales por color
    # para no inventar el cruce a cliente).
    color_rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(COALESCE(cod, ''))) AS color,
               COALESCE(SUM(COALESCE(toper, 0)
                          + COALESCE(jersey, 0)
                          + COALESCE(pique, 0)), 0) AS kg_total
          FROM scintela.tinto
         WHERE fecha >= %s
         GROUP BY UPPER(TRIM(COALESCE(cod, '')))
        """,
        (desde,),
    ) or []
    kg_por_color = {r["color"]: float(r["kg_total"] or 0)
                    for r in color_rows
                    if (r.get("color") or "") in COLORES_LEGACY}

    # Adjuntamos un dict de colores VACIO por cliente — la base no tiene
    # forma directa de cruzar cliente↔color en este esquema.
    filas = [
        {
            "codigo_cli":  c["codigo_cli"],
            "nombre":      c["nombre"],
            "saldo_total": float(c["saldo_total"] or 0),
            # Vacío por cliente — se llena si el usuario decide portar PIEZAS.
            "colores":     {col: 0.0 for col in COLORES_LEGACY},
        }
        for c in clientes
    ]

    return {
        "filas":            filas,
        "colores_orden":    list(COLORES_LEGACY),
        "kg_por_color_global": kg_por_color,
        "fuente_color":     "scintela.tinto.cod",
        "necesita_decision": True,
        "nota_decision": (
            "scintela.tinto NO tiene codigo_cli — el cruce cliente↔color "
            "del dBase legacy (INFORMES.PRG L830-918) leía de F:\\TINTELA\\"
            "PIEZAS, una tabla de control de piezas físicas con cliente Y "
            "color juntos. Esa tabla no se importó al schema PG. Decisión "
            "pendiente: (a) portar PIEZAS.DBF a scintela.piezas con migrate; "
            "(b) agregar columna codigo_cli a scintela.tinto y cargarla al "
            "facturar; (c) calcular el cruce via JOIN factura ↔ orden_de_"
            "trabajo si la OT registra colores. La vista por ahora muestra "
            "totales globales por color + cartera por cliente sin cruzar."
        ),
        "meses_atras":      meses_atras_int,
        "fecha_desde":      desde.isoformat(),
    }


def aplicar_stop_automatico(umbral_dias: int = 90, usuario: str = "web") -> dict:
    """Marca stop='S' a todo cliente con facturas vencidas > umbral_dias.

    Idempotente — sólo toca clientes que aún no están en stop (el filtro
    `stop != 'S'` vive en `clientes_con_vencido`, no acá).
    Un solo `db.tx()`: o todos los clientes pasan a stop o ninguno.
    Deja traza en `cliente.observacion` para saber por qué quedaron
    bloqueados cuando uno los mire después.
    """
    victimas = clientes_con_vencido(umbral_dias)
    if not victimas:
        return {"n": 0, "codigos": [], "detalle": []}

    marca = f"[S] STOP AUTO {date.today().isoformat()} (>{umbral_dias}d)"
    codigos = [v["codigo_cli"] for v in victimas]
    with db.tx() as conn:
        db.execute(
            """
            UPDATE scintela.cliente
               SET stop = 'S',
                   observacion = COALESCE(observacion || ' | ', '') || %s,
                   usuario_modifica = %s
             WHERE codigo_cli = ANY(%s)
               AND COALESCE(stop, 'N') != 'S'
            """,
            (marca, usuario, codigos),
            conn=conn,
        )
    return {
        "n": len(codigos),
        "codigos": codigos,
        "detalle": victimas,
    }
