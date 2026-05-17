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
# El orden se calcula con CASE en SQL para que el ordenamiento sea estable
# y consistente entre vistas. Los códigos de tipo son los que aparecen en
# el dump histórico — la búsqueda es case-insensitive.
_CATEGORIA_CASE_SQL = """
    CASE
      -- Inmuebles: terreno, edificio, instalaciones.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('TER','EDF','INS','TERRENO','PRED','PROP','TERRENOS','EDIFICIO','INSTALACIONES')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(TERRENO|PREDIO|LOTE|PROPIEDAD|FINCA|EDIFICIO|INSTALAC|BODEGA)'
        THEN 1
      -- Producción: incluye secciones específicas (HIL/TEJ/TIN/QUI/ACA) +
      -- maquinaria genérica + herramientas. TMT 2026-05-17.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('MAQ','HER','EQO','HIL','TEJ','TIN','QUI','ACA','MAQUINARIA','MAQUINA','MAQUINAS','HERRAMIENTAS','EQUIPOS','HILADO','TEJEDURIA','TINTURA','QUIMICOS','ACABADO')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(MAQUINA|TELAR|TINTE|RAMA|TERMOFI|HERRAMIENT|HILADO|TEJED|TINTUR|QUIMIC|ACABAD)'
        THEN 2
      -- Vehículos.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('VEH','CAR','VEHICULO','AUTO','CAMION','CAMIONETA','VEHICULOS')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(VEHICULO|CAMION|AUTO|CAMIONETA|MOTO)'
        THEN 3
      -- Oficina: cómputo, muebles, software.
      WHEN UPPER(COALESCE(a.tipo, '')) IN ('OFI','MUE','SFW','OFICINA','COMP','COMPUTO','MUEBLES','SOFTWARE')
        OR UPPER(COALESCE(a.concepto, '')) ~ '(COMPUTAD|IMPRESORA|MUEBLE|OFICINA|SOFTWARE|INTANGIB)'
        THEN 4
      ELSE 5
    END
"""

CATEGORIA_LABELS = {
    1: "Terrenos y propiedades",
    2: "Maquinaria",
    3: "Vehículos",
    4: "Equipo de oficina",
    5: "Otros",
}


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
    sql = f"""
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c
        )
        SELECT a.id_activos,
               a.fecha,
               a.concepto,
               a.tipo,
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
        ORDER BY {_CATEGORIA_CASE_SQL} ASC,
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
