"""Queries de activos fijos.

Modelo:
    inicial      = valor de compra
    amortizac    = depreciación acumulada (suma de amortimes mes a mes)
    amortimes    = depreciación mensual fija (lo que la proc resta cada mes)
    valor        = valor en libros (= inicial - amortizac)
    vida_util    = meses de vida útil
    ult_mes_amortizado = año*100+mes del último ciclo aplicado (idempotencia)

La proc `scintela.actualizar_amortizacion()` mira ult_mes_amortizado vs
el mes actual; si ya corrió, no hace nada. Se la puede correr cada vez
que querés sin riesgo de doble-amortizar.

Orden canónico de listado (2026-04-29 — ver docs/SKILL_ADDENDUM_BATCH_18.md):
    1. Terrenos y propiedades
    2. Maquinaria
    3. Vehículos
    4. Equipo de oficina
    5. Otros

La discriminación se hace por `tipo` (matching contra alias conocidos)
con fallback a búsqueda en `concepto`. Se expone un `categoria_orden`
numérico (1-5) y un `categoria_label` legible para que la lista los
agrupe con headers.
"""
from __future__ import annotations

from datetime import date

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta

# Mapeo canónico de tipo → orden + label.
# TMT 2026-05-20 — pedido dueña: códigos de una letra T/I/M/K/C y orden
# fijo:
#   T = Terrenos             (1)
#   I = Edificios            (2)
#   M = Maquinaria Tintorería (3)
#   K = Maquinaria Tejeduría  (4)
#   C = Camiones             (5)
#
# Los códigos legacy (TER/EDF/HIL/TEJ/TIN/MAQ/VEH) siguen funcionando como
# fallback — la dueña los va a ir migrando con el inline-edit. Cualquier
# tipo no reconocido cae en 99 = "Otros".
_CATEGORIA_CASE_SQL = """
    CASE
      -- 1. Terrenos
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('T','TER','PROP','TERRENO','TERRENOS','PRED','LOTE')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(TERRENO|PREDIO|LOTE|FINCA|PROPIEDAD)'
        THEN 1
      -- 2. Edificios (e instalaciones de inmueble: bodegas, etc.)
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('I','EDF','INS','EDIFICIO','EDIFICIOS','INSTALACIONES')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(EDIFICIO|INSTALAC|BODEGA)'
        THEN 2
      -- 3. Maquinaria Tintorería: tinte, química, acabado.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('M','TIN','QUI','ACA','TINTURA','TINTORERIA','QUIMICOS','ACABADO')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(TINTUR|TINTORERIA|QUIMIC|ACABAD|RAMA|TERMOFI)'
        THEN 3
      -- 4. Maquinaria Tejeduría: hilado, tejido, telares.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('K','HIL','TEJ','HILADO','TEJEDURIA')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(HILADO|TEJED|TELAR|URDIDORA)'
        THEN 4
      -- 5. Camiones (vehículos motorizados).
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('C','VEH','CAR','CAMION','CAMIONES','VEHICULO','VEHICULOS','AUTO','CAMIONETA')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(CAMION|VEHICULO|AUTO|CAMIONETA|MOTO)'
        THEN 5
      ELSE 99
    END
"""

# Etiqueta legible para cada bucket (TMT 2026-05-20).
CATEGORIA_LABELS = {
    1: "Terrenos",
    2: "Edificios",
    3: "Maquinaria Tintorería",
    4: "Maquinaria Tejeduría",
    5: "Camiones",
    99: "Otros",
}

# Códigos canónicos para el dropdown de inline-edit (sin Otros — la dueña
# elige uno de los 5).
TIPOS_CANONICOS = [
    ("T", "T · Terrenos"),
    ("I", "I · Edificios"),
    ("M", "M · Maquinaria Tintorería"),
    ("K", "K · Maquinaria Tejeduría"),
    ("C", "C · Camiones"),
]


# Cache module-level del feature flag "existe scintela.activos.orden_manual"
# (columna que trae la migración 0037). Sin esta cache haríamos un check
# information_schema por cada request. La invalidación natural es el
# restart del worker — si la columna se crea, basta con restart-task.
_HAS_ORDEN_MANUAL: bool | None = None


def _tiene_orden_manual() -> bool:
    """Detecta si scintela.activos.orden_manual existe — feature flag para
    la migración 0037. Defensivo: durante el gap entre deploy y migrate,
    /activos seguía funcionando sin el column."""
    global _HAS_ORDEN_MANUAL
    if _HAS_ORDEN_MANUAL is not None:
        return _HAS_ORDEN_MANUAL
    try:
        row = db.fetch_one(
            """
            SELECT 1 AS x FROM information_schema.columns
             WHERE table_schema = 'scintela'
               AND table_name   = 'activos'
               AND column_name  = 'orden_manual'
            """
        )
        _HAS_ORDEN_MANUAL = row is not None
    except Exception:  # noqa: BLE001
        _HAS_ORDEN_MANUAL = False
    return _HAS_ORDEN_MANUAL


def buscar(
    q: str = "",
    tipo: str | None = None,
    solo_activos: bool = False,
    limite: int = 500,
) -> list[dict]:
    """Lista de activos. `solo_activos=True` filtra los ya totalmente
    amortizados (valor en libros = 0).

    Devuelve además dos columnas calculadas:
        pct_depreciado = amortizac / inicial * 100
        valor_libros   = inicial - amortizac (mismo que `valor` cuando
                         está sincronizado, pero lo recalculamos para no
                         depender del trigger).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # COEF = min(día_del_mes, 30) / 30  →  proración diaria (MENU.PRG L275).
    # AMORTIMES_calc = COEF × CUOTA  (lo que va corriendo este mes).
    # valor_libros = inicial - amortizac_acum - amortimes_calc.
    #
    # TMT 2026-05-20 v3: la dueña pidió sort puro por categoría → fecha.
    # `orden_manual` (de la migración 0037) ya no entra en el ORDER BY
    # para evitar que los headers de categoría se dupliquen. La columna
    # se mantiene en SELECT por si en el futuro reactivamos el drag-drop.
    if _tiene_orden_manual():
        orden_manual_select   = "a.orden_manual,"
        orden_manual_order_by = ""  # ya no se usa en ORDER BY
    else:
        orden_manual_select   = ""
        orden_manual_order_by = ""
    sql = f"""
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::numeric, 30) / 30.0 AS c
        )
        SELECT a.id_activos,
               a.fecha,
               a.concepto,
               a.tipo,
               {orden_manual_select}
               a.inicial,
               a.amortizac,
               -- AMORTIMES calculado (no el stored): COEF × cuota
               ROUND(((SELECT c FROM coef) * COALESCE(a.cuota, 0))::numeric, 2)
                                                                     AS amortimes,
               -- TMT 2026-05-27 dueña: depreciación diaria = cuota/30.
               -- Cada día corrido del mes este monto se "consume" de la utilidad.
               ROUND((COALESCE(a.cuota, 0) / 30.0)::numeric, 2)        AS deprec_dia,
               -- VALOR en libros = inicial - amortizac - amortimes_calc
               GREATEST(
                 COALESCE(a.inicial, 0)
                   - COALESCE(a.amortizac, 0)
                   - (SELECT c FROM coef) * COALESCE(a.cuota, 0),
                 0
               )                                                     AS valor,
               a.cuota,
               a.vida_util,
               a.ult_mes_amortizado,
               a.id_proveedor,
               COALESCE(p.nombre, '') AS proveedor,
               GREATEST(
                 COALESCE(a.inicial, 0)
                   - COALESCE(a.amortizac, 0)
                   - (SELECT c FROM coef) * COALESCE(a.cuota, 0),
                 0
               )                                                     AS valor_libros,
               CASE WHEN COALESCE(a.inicial, 0) > 0
                    THEN ROUND(
                          100.0 * (
                            COALESCE(a.amortizac, 0)
                            + (SELECT c FROM coef) * COALESCE(a.cuota, 0)
                          ) / a.inicial, 1)
                    ELSE 0 END                                       AS pct_depreciado,
               {_CATEGORIA_CASE_SQL}                                 AS categoria_orden
        FROM scintela.activos a
        LEFT JOIN scintela.proveedor p ON p.id_proveedor = a.id_proveedor
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(a.concepto, '')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(a.tipo, '')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(p.nombre, '')) LIKE UPPER(%(like)s))
          AND (%(tipo)s IS NULL OR UPPER(a.tipo) = UPPER(%(tipo)s))
          AND (NOT %(solo_activos)s
               OR (COALESCE(a.inicial, 0) - COALESCE(a.amortizac, 0)) > 0.01)
        -- TMT 2026-05-20 v3: pedido dueña "Ordenar por tipo luego por
        -- fecha. Solo una vez cada sub categoria". Sort estricto =
        -- categoría → fecha DESC. orden_manual queda como secundario
        -- DENTRO de cada categoría (ya no rompe el group-by-categoría —
        -- antes los rows arrastrados saltaban entre categorías y los
        -- headers se duplicaban).
        ORDER BY {_CATEGORIA_CASE_SQL} ASC,
                 {orden_manual_order_by}
                 a.fecha DESC NULLS LAST, a.id_activos DESC
        LIMIT %(limite)s
    """
    filas = db.fetch_all(
        sql,
        {
            "q": q or None, "like": like,
            "tipo": tipo or None,
            "solo_activos": bool(solo_activos),
            "limite": limite,
        },
    ) or []
    # Etiqueta legible — Python-side, no SQL, para no acoplar el lookup.
    for f in filas:
        f["categoria_label"] = CATEGORIA_LABELS.get(
            int(f.get("categoria_orden") or 5), "Otros"
        )
    return filas


def tipos_disponibles() -> list[dict]:
    """Lista los tipos distintos para el filtro — con conteo."""
    return db.fetch_all(
        """
        SELECT COALESCE(NULLIF(TRIM(tipo), ''), '(s/t)') AS tipo,
               COUNT(*)                                  AS n
        FROM scintela.activos
        GROUP BY 1
        ORDER BY n DESC, 1
        """
    ) or []


def resumen() -> dict:
    """KPIs: total inicial, amortizado acumulado, valor en libros, # activos.

    Valor en libros y cuota del mes se prorratean por día (igual que el
    cálculo línea-por-línea en `buscar()`): el día 15 ya descontamos
    medio cuota del mes; el día 30+ descontamos el mes entero.
    """
    row = db.fetch_one(
        """
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::numeric, 30) / 30.0 AS c
        )
        SELECT COUNT(*)                                            AS n,
               COALESCE(SUM(inicial), 0)                           AS inicial,
               COALESCE(SUM(amortizac), 0)                         AS amortizado,
               -- cuota_mes prorrateada (no la stored): COEF × cuota
               COALESCE(SUM((SELECT c FROM coef) * COALESCE(cuota, 0)), 0)
                                                                   AS cuota_mes,
               -- TMT 2026-05-27 dueña: total depreciación diaria
               -- (suma de cuota/30 por activo). Total mensual = SUM(cuota).
               COALESCE(SUM(COALESCE(cuota, 0)) / 30.0, 0)         AS deprec_dia_total,
               COALESCE(SUM(COALESCE(cuota, 0)), 0)                AS deprec_mes_total,
               -- valor en libros con prorrateo diario
               COALESCE(SUM(GREATEST(
                 COALESCE(inicial, 0)
                   - COALESCE(amortizac, 0)
                   - (SELECT c FROM coef) * COALESCE(cuota, 0),
                 0
               )), 0)                                              AS valor_libros,
               COUNT(*) FILTER (WHERE
                 COALESCE(inicial, 0)
                   - COALESCE(amortizac, 0)
                   - (SELECT c FROM coef) * COALESCE(cuota, 0)
                 > 0.01
               )                                                   AS n_vivos
        FROM scintela.activos
        """
    )
    if not row:
        return {
            "n": 0, "n_vivos": 0,
            "inicial": 0.0, "amortizado": 0.0,
            "cuota_mes": 0.0, "valor_libros": 0.0,
            "deprec_dia_total": 0.0, "deprec_mes_total": 0.0,
        }
    return {
        "n":                int(row.get("n") or 0),
        "n_vivos":          int(row.get("n_vivos") or 0),
        "inicial":          float(row.get("inicial") or 0),
        "amortizado":       float(row.get("amortizado") or 0),
        "cuota_mes":        float(row.get("cuota_mes") or 0),
        "valor_libros":     float(row.get("valor_libros") or 0),
        "deprec_dia_total": float(row.get("deprec_dia_total") or 0),
        "deprec_mes_total": float(row.get("deprec_mes_total") or 0),
    }


def crear(
    *,
    fecha: date,
    concepto: str,
    tipo: str,
    inicial,
    vida_util_meses: int,
    cuota=None,
    id_proveedor: int | None = None,
    usuario: str = "web",
) -> dict:
    """Alta de un activo fijo.

    Args:
        fecha: fecha de compra (usada como base para amortización).
        concepto: descripción del activo (ej. "Telar Sulzer #3").
        tipo: código de 3 letras (TER/MAQ/VEH/OFI/OTR).
        inicial: valor de compra en USD.
        vida_util_meses: meses de vida útil.
        cuota: depreciación mensual fija. Si None, se calcula =
               inicial / vida_util (cuotas iguales mes a mes).
        id_proveedor: FK opcional a scintela.proveedor.

    Crea con amortizac=0, amortimes=0, valor=inicial, ult_mes_amortizado=NULL.
    La proc `actualizar_amortizacion()` aplica la cuota mensual desde el
    próximo cierre de mes. TMT 2026-05-17.
    """
    concepto = (concepto or "").strip()
    if not concepto:
        raise ValueError("El concepto/descripción es obligatorio.")
    tipo = (tipo or "").strip().upper()[:3]
    if not tipo:
        raise ValueError("Elegí un tipo (Maquinaria / Vehículo / etc).")
    importe_inicial = float(inicial or 0)
    if importe_inicial <= 0:
        raise ValueError("El valor inicial debe ser mayor a cero.")
    vida_util_meses = int(vida_util_meses or 0)
    if vida_util_meses <= 0:
        raise ValueError("La vida útil (meses) debe ser mayor a cero.")

    # Cuota: si no viene, calcular = inicial / vida_util.
    if cuota is None or float(cuota or 0) <= 0:
        cuota_f = round(importe_inicial / vida_util_meses, 2)
    else:
        cuota_f = float(cuota)
        if cuota_f * vida_util_meses < importe_inicial * 0.5:
            raise ValueError(
                f"La cuota ${cuota_f:.2f} × {vida_util_meses} meses = "
                f"${cuota_f * vida_util_meses:.2f}, menos de la mitad del "
                f"valor inicial ${importe_inicial:.2f}. Revisá."
            )

    row = db.execute_returning(
        """
        INSERT INTO scintela.activos
            (fecha, concepto, tipo, inicial, amortizac, amortimes, valor,
             cuota, vida_util, id_proveedor, usuario_crea)
        VALUES (%s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s)
        RETURNING id_activos
        """,
        (
            fecha, concepto[:100], tipo, importe_inicial,
            importe_inicial, cuota_f, vida_util_meses,
            id_proveedor, usuario[:50],
        ),
    )
    return {
        "id_activos":   int(row["id_activos"]) if row else 0,
        "concepto":     concepto,
        "cuota":        cuota_f,
        "vida_util":    vida_util_meses,
        "inicial":      importe_inicial,
    }


def editar_tipo(id_activo: int, tipo_nuevo: str, *, usuario: str = "web") -> dict:
    """Cambia el `tipo` de un activo. TMT 2026-05-20.

    Acepta los 5 códigos canónicos (T/I/M/K/C) — cualquier otro string
    no vacío también pasa (compat con legacy "TER", "MAQ", etc.), pero
    el inline-edit del template sólo ofrece los 5 nuevos.

    Devuelve `{id_activos, tipo, categoria_orden, categoria_label}` para
    que el front-end actualice la fila sin recargar.
    """
    tipo_nuevo = (tipo_nuevo or "").strip().upper()[:3]
    if not tipo_nuevo:
        raise ValueError("Tipo requerido.")

    n = db.execute(
        "UPDATE scintela.activos SET tipo = %s WHERE id_activos = %s",
        (tipo_nuevo, id_activo),
    )
    if not n:
        raise ValueError(f"Activo id={id_activo} no existe.")

    # Releer la fila para devolver el bucket de categoría actualizado.
    sql = f"""
        SELECT id_activos, tipo, concepto,
               {_CATEGORIA_CASE_SQL} AS categoria_orden
          FROM scintela.activos a
         WHERE id_activos = %s
    """
    row = db.fetch_one(sql, (id_activo,)) or {}
    cat = int(row.get("categoria_orden") or 99)
    return {
        "id_activos":       int(row.get("id_activos") or id_activo),
        "tipo":             row.get("tipo") or tipo_nuevo,
        "categoria_orden":  cat,
        "categoria_label":  CATEGORIA_LABELS.get(cat, "Otros"),
    }


def reordenar(ids_en_orden: list[int], *, usuario: str = "web") -> int:
    """Persiste el orden manual de activos.

    TMT 2026-05-20 — pedido dueña: "Dejame drag and drop en activos.
    porque asi lo ordeno manualmente".

    Recibe la lista de id_activos EN EL ORDEN VISIBLE. A cada uno le
    asigna `orden_manual = índice + 1` (empieza en 1 para que NULL siga
    siendo "sin orden"). Todo en una sola transacción.

    Si la columna no existe (migración 0037 sin correr), levanta
    ValueError — el caller debe haber chequeado `_tiene_orden_manual()`
    antes de invocar.
    """
    if not _tiene_orden_manual():
        raise ValueError(
            "scintela.activos.orden_manual no existe — correr la "
            "migración 0037 antes de usar el drag-and-drop."
        )
    if not ids_en_orden:
        return 0
    # Sanitizar — sólo ints, dedupe preservando orden.
    seen: set[int] = set()
    ids_clean: list[int] = []
    for raw in ids_en_orden:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if i in seen:
            continue
        seen.add(i)
        ids_clean.append(i)
    if not ids_clean:
        return 0
    with db.tx() as conn, conn.cursor() as cur:
        for idx, id_activo in enumerate(ids_clean, start=1):
            cur.execute(
                """
                UPDATE scintela.activos
                   SET orden_manual = %s
                 WHERE id_activos = %s
                """,
                (idx, id_activo),
            )
    return len(ids_clean)


def _sumar_meses(d: date, n: int) -> date:
    """Suma N meses a una fecha. Si el día destino no existe en el mes
    (ej: 31 de febrero), cae al último día del mes."""
    from calendar import monthrange
    total_m = d.month - 1 + n
    y = d.year + total_m // 12
    m = total_m % 12 + 1
    last = monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def activar_maquinaria(
    *,
    codigo_prov: str,
    ids_anticipos: list[int],
    concepto: str,
    tipo: str,
    valor_total: float,
    vida_util_meses: int,
    n_cuotas: int,
    meses_entre_cuotas: int,
    fecha_primera_cuota: date | None,
    usuario: str = "web",
) -> dict:
    """Activa una máquina recién llegada en una sola transacción atómica.

    Pedido dueña 2026-05-20: el flujo de "activación de maquinaria" es:
      1. Marcar los anticipos USD seleccionados como consumidos (`st='M'`).
      2. Insertar el activo (scintela.activos) con valor=valor_total y
         cuota mensual = valor_total / vida_util_meses.
      3. Insertar N posdats (una por cuota) escalonadas cada
         `meses_entre_cuotas`, sumando la **deuda residual**.
      4. Registrar mov_doble linkeado para audit + reverso futuro.

    **La deuda es residual** (no se ingresa): `deuda = valor_total - SUM(anticipos)`.
    El form muestra el cálculo en vivo; este queries lo recomputa
    server-side para no confiar en lo que mande el cliente.

    Validaciones:
      - Anticipos todos del MISMO proveedor + todos vivos.
      - `valor_total >= SUM(anticipos)` (si no, error: "los anticipos
        superan el valor — no se pueden activar máquinas con cambio").
      - Si la deuda > 0: requiere `n_cuotas >= 1` y `fecha_primera_cuota`.
      - Si la deuda == 0: no se crean posdats.

    Devuelve `{id_activos, ids_posdat: [...], n_anticipos_consumidos,
              valor_total, deuda_total, n_cuotas}`.
    """
    import db as _db

    # ── 0. Sanitizar y validar inputs básicos ───────────────────────────
    codigo_prov = (codigo_prov or "").strip().upper()
    if not codigo_prov:
        raise ValueError("Proveedor requerido.")
    concepto = (concepto or "").strip()
    if not concepto:
        raise ValueError("Concepto / nombre de la máquina requerido.")
    tipo = (tipo or "").strip().upper()[:3]
    if not tipo:
        raise ValueError("Tipo requerido (T/I/M/K/C).")
    valor_total = float(valor_total or 0)
    vida_util_meses = int(vida_util_meses or 0)
    n_cuotas = int(n_cuotas or 0)
    meses_entre_cuotas = int(meses_entre_cuotas or 0)
    if valor_total <= 0:
        raise ValueError("Valor total de la máquina debe ser > 0.")
    if vida_util_meses <= 0:
        raise ValueError("Vida útil debe ser > 0 meses.")

    ids_unique = sorted({int(i) for i in ids_anticipos if i})
    hoy = today_ec()
    asegurar_fecha_abierta(hoy)

    with _db.tx() as conn:
        # ── 1. Validar proveedor ────────────────────────────────────────
        prov_row = _db.fetch_one(
            "SELECT id_proveedor, codigo_prov, COALESCE(nombre,'') AS nombre "
            "FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,), conn=conn,
        )
        if not prov_row:
            raise ValueError(f"Proveedor {codigo_prov!r} no existe.")
        id_proveedor = int(prov_row["id_proveedor"])

        # ── 2. Validar anticipos: mismo prov + todos vivos + suma ─────
        suma_anticipos = 0.0
        if ids_unique:
            placeholder = ",".join(["%s"] * len(ids_unique))
            rows = _db.fetch_all(
                f"""
                SELECT id_dolares, cta, importe, st
                  FROM scintela.dolares
                 WHERE id_dolares IN ({placeholder})
                 ORDER BY id_dolares
                 FOR UPDATE
                """,
                tuple(ids_unique), conn=conn,
            ) or []
            if len(rows) != len(ids_unique):
                faltan = set(ids_unique) - {int(r["id_dolares"]) for r in rows}
                raise ValueError(f"No encontré anticipos: {sorted(faltan)}.")
            for r in rows:
                cta_r = (r.get("cta") or "").strip().upper()
                if cta_r != codigo_prov:
                    raise ValueError(
                        f"Anticipo #{r['id_dolares']} es del proveedor "
                        f"{cta_r!r}, no {codigo_prov!r}."
                    )
                if (r.get("st") or "").strip():
                    raise ValueError(
                        f"Anticipo #{r['id_dolares']} ya está consumido "
                        f"(st='{r.get('st')}')."
                    )
                suma_anticipos += float(r.get("importe") or 0)

        # ── 3. Deuda residual: valor - anticipos. NO viene del cliente. ──
        deuda_total = round(valor_total - suma_anticipos, 2)
        if deuda_total < -0.005:
            raise ValueError(
                f"Los anticipos ({suma_anticipos:.2f}) superan el valor "
                f"total ({valor_total:.2f}). No se puede activar una "
                f"máquina con cambio a favor del proveedor."
            )
        # Snap a cero por si la diferencia es < 0.5 cent (rounding).
        if abs(deuda_total) < 0.005:
            deuda_total = 0.0

        # Si hay deuda, validar parámetros de las cuotas.
        if deuda_total > 0:
            if n_cuotas < 1:
                raise ValueError("Hay deuda residual — n° de cuotas debe ser >= 1.")
            if meses_entre_cuotas < 1:
                raise ValueError("Meses entre cuotas debe ser >= 1.")
            if not fecha_primera_cuota:
                raise ValueError("Fecha de primera cuota requerida.")

        # ── 4. Marcar anticipos como consumidos (st='M') ───────────────
        if ids_unique:
            _db.execute(
                f"""
                UPDATE scintela.dolares
                   SET st = 'M',
                       usuario_modifica = %s,
                       fecha_modifica = CURRENT_TIMESTAMP
                 WHERE id_dolares IN ({",".join(["%s"] * len(ids_unique))})
                """,
                (usuario[:50], *ids_unique),
                conn=conn,
            )

        # ── 5. INSERT activo ────────────────────────────────────────────
        cuota_mensual = round(valor_total / vida_util_meses, 2)
        activo_row = _db.execute_returning(
            """
            INSERT INTO scintela.activos
                (fecha, concepto, tipo, inicial, amortizac, amortimes,
                 valor, cuota, vida_util, id_proveedor, usuario_crea)
            VALUES (%s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s)
            RETURNING id_activos
            """,
            (
                hoy, concepto[:100], tipo, valor_total,
                valor_total, cuota_mensual, vida_util_meses,
                id_proveedor, usuario[:50],
            ),
            conn=conn,
        ) or {}
        id_activos = int(activo_row.get("id_activos") or 0)

        # ── 6. INSERT N posdats (uno por cuota) ─────────────────────────
        ids_posdat: list[int] = []
        if deuda_total > 0 and n_cuotas > 0:
            # Importe por cuota: distribuir exacto. La última absorbe
            # el resto del round.
            base = round(deuda_total / n_cuotas, 2)
            ajuste_ultima = round(deuda_total - base * n_cuotas, 2)
            # Próximo num correlativo para posdat.
            row_n = _db.fetch_one(
                "SELECT COALESCE(MAX(num), 0) + 1 AS sig FROM scintela.posdat",
                conn=conn,
            )
            num_base = int(row_n["sig"]) if row_n else 1
            for i in range(n_cuotas):
                num_i = num_base + i
                fechad_i = _sumar_meses(fecha_primera_cuota, i * meses_entre_cuotas)
                imp_i = base + (ajuste_ultima if i == n_cuotas - 1 else 0)
                concepto_i = (
                    f"Cuota {i + 1}/{n_cuotas} maq {concepto}"
                )[:100]
                pr = _db.execute_returning(
                    """
                    INSERT INTO scintela.posdat
                        (num, fecha, fechad, prov, importe, banc, concepto,
                         usuario_crea)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                    RETURNING id_posdat
                    """,
                    (num_i, hoy, fechad_i, codigo_prov, imp_i,
                     concepto_i, usuario[:50]),
                    conn=conn,
                ) or {}
                if pr.get("id_posdat"):
                    ids_posdat.append(int(pr["id_posdat"]))

        # ── 7. mov_doble del evento atómico ─────────────────────────────
        try:
            import uuid as _uuid

            import mov_doble as _md
            batch_id = str(_uuid.uuid4())
            # Una fila resumen del evento (audit completo).
            _md.registrar(
                conn=conn,
                tipo="activacion_maquinaria",
                origen_table="activos",
                origen_id=id_activos,
                destino_table="activos",
                destino_id=id_activos,
                importe=valor_total,
                fecha=hoy,
                concepto=(
                    f"Activación máquina {concepto} · proveedor {codigo_prov} · "
                    f"valor ${valor_total:.2f} · anticipos ${suma_anticipos:.2f} · "
                    f"deuda ${deuda_total:.2f} en {n_cuotas} cuotas"
                )[:200],
                usuario=usuario,
                batch_id=batch_id,
                metadata={
                    "codigo_prov":      codigo_prov,
                    "id_activos":       id_activos,
                    "valor_total":      valor_total,
                    "anticipos":        suma_anticipos,
                    "deuda_total":      deuda_total,
                    "n_cuotas":         n_cuotas,
                    "meses_entre":      meses_entre_cuotas,
                    "ids_anticipos":    ids_unique,
                    "ids_posdat":       ids_posdat,
                    "vida_util_meses":  vida_util_meses,
                    "cuota_mensual":    cuota_mensual,
                },
            )
        except Exception:  # noqa: BLE001
            # mov_doble es para audit/reverso — si falla NO abortamos
            # la transacción (la activación ya quedó). Pero burbujamos
            # por si es un error inesperado.
            raise

    return {
        "id_activos":             id_activos,
        "ids_posdat":             ids_posdat,
        "n_anticipos_consumidos": len(ids_unique),
        "valor_total":            valor_total,
        "deuda_total":            deuda_total,
        "n_cuotas":               n_cuotas,
        "cuota_mensual":          cuota_mensual,
    }


def correr_amortizacion(usuario: str = "web") -> dict:
    """Llama la procedure `scintela.actualizar_amortizacion()`.

    La proc es idempotente vía `ult_mes_amortizado`. Si el mes corriente
    ya fue procesado, no toca ninguna fila y devuelve 0 cambios.

    Devuelve `{ejecutada: True, mes: 'YYYY-MM', filas_tocadas: N}`.
    """
    mes = today_ec().strftime("%Y-%m")
    filas_antes = db.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM scintela.activos
        WHERE ult_mes_amortizado IS NOT NULL
          AND ult_mes_amortizado >= EXTRACT(YEAR FROM CURRENT_DATE) * 100
                                  + EXTRACT(MONTH FROM CURRENT_DATE)
        """
    ) or {}
    n_antes = int(filas_antes.get("n") or 0)

    with db.tx() as conn, conn.cursor() as cur:
        cur.execute("SELECT scintela.actualizar_amortizacion()")

    filas_despues = db.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM scintela.activos
        WHERE ult_mes_amortizado IS NOT NULL
          AND ult_mes_amortizado >= EXTRACT(YEAR FROM CURRENT_DATE) * 100
                                  + EXTRACT(MONTH FROM CURRENT_DATE)
        """
    ) or {}
    n_despues = int(filas_despues.get("n") or 0)

    return {
        "ejecutada":     True,
        "mes":           mes,
        "filas_tocadas": max(0, n_despues - n_antes),
        "ya_estaba":     n_antes > 0 and n_despues == n_antes,
    }
