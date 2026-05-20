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
          SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c
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
          SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c
        )
        SELECT COUNT(*)                                            AS n,
               COALESCE(SUM(inicial), 0)                           AS inicial,
               COALESCE(SUM(amortizac), 0)                         AS amortizado,
               -- cuota_mes prorrateada (no la stored): COEF × cuota
               COALESCE(SUM((SELECT c FROM coef) * COALESCE(cuota, 0)), 0)
                                                                   AS cuota_mes,
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
        }
    return {
        "n":            int(row.get("n") or 0),
        "n_vivos":      int(row.get("n_vivos") or 0),
        "inicial":      float(row.get("inicial") or 0),
        "amortizado":   float(row.get("amortizado") or 0),
        "cuota_mes":    float(row.get("cuota_mes") or 0),
        "valor_libros": float(row.get("valor_libros") or 0),
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


def correr_amortizacion(usuario: str = "web") -> dict:
    """Llama la procedure `scintela.actualizar_amortizacion()`.

    La proc es idempotente vía `ult_mes_amortizado`. Si el mes corriente
    ya fue procesado, no toca ninguna fila y devuelve 0 cambios.

    Devuelve `{ejecutada: True, mes: 'YYYY-MM', filas_tocadas: N}`.
    """
    mes = date.today().strftime("%Y-%m")
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
