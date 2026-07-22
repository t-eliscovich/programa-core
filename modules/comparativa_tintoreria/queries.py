"""Queries para /informes/comparativa-tintoreria.

Solo PC (scintela.tinto). El lado formulas_app lo provee
`modules.tintura.service.tinturado_resumen`.
"""
from __future__ import annotations

from datetime import date

import db
from filters import today_ec


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
    # CORTE tintura 2026-07-07: los lotes ANTERIORES al corte salían del dBase
    # (scintela.tinto); del corte en adelante, de formulas_app. Se mergea en
    # Python porque formulas_app vive en otra base (no se puede UNION en SQL).
    # TMT 2026-07-16 (dueña): el dBase dejó de cargar el tinto en el cutover, así
    # que su parte del MES de corte (01–06/07) quedó vacía → se perdía la primera
    # semana (~48k). Ahora el split es el PRIMER DÍA DEL MES de corte: el mes de
    # corte en adelante sale ENTERO de formulas (que sí tiene el mes completo);
    # el dBase solo aporta meses estrictamente anteriores.
    from modules.informes.queries import CORTE_TINTURA
    _corte_mes = date(CORTE_TINTURA.year, CORTE_TINTURA.month, 1)

    rows = db.fetch_all(
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
               AND fecha < %s
        )
        SELECT yy, mm, tipo,
               COALESCE(SUM(kg_n), 0) AS kg,
               COALESCE(SUM(imp), 0)  AS importe
          FROM clasif
         GROUP BY yy, mm, tipo
         ORDER BY yy, mm, tipo
        """,
        (limite_bajos, desde, hasta, _corte_mes),
    ) or []

    agg: dict = {}
    for r in rows:
        agg[(int(r["yy"]), int(r["mm"]), r["tipo"])] = {
            "yy": int(r["yy"]), "mm": int(r["mm"]), "tipo": r["tipo"],
            "kg": float(r["kg"] or 0), "importe": float(r["importe"] or 0),
        }

    # formulas_app: del MES de corte en adelante, misma regla Bajos/Fuertes.
    f_desde = max(desde, _corte_mes)
    if f_desde <= hasta:
        try:
            from modules.tintura import service as _tint_svc

            for o in _tint_svc.tinto_equiv_formulas(f_desde, hasta):
                if not o.fecha:
                    continue
                kg_bruto = o.kg or 0.0
                if kg_bruto <= 0:  # igual que el guard AND kg>0 del dBase
                    continue
                kg_n = (o.kgn if o.kgn is not None else o.kg) or 0.0
                imp = o.importe or 0.0
                tipo = "Bajos" if (imp / kg_bruto) <= limite_bajos else "Fuertes"
                k = (o.fecha.year, o.fecha.month, tipo)
                cur = agg.setdefault(k, {
                    "yy": o.fecha.year, "mm": o.fecha.month,
                    "tipo": tipo, "kg": 0.0, "importe": 0.0,
                })
                cur["kg"] += kg_n
                cur["importe"] += imp
        except Exception:  # noqa: BLE001 -- fail-soft
            pass

    return sorted(agg.values(), key=lambda d: (d["yy"], d["mm"], d["tipo"]))


def _amortizacion_dcc_por_mes(desde: date, hasta: date) -> dict:
    """Replica DCC = deprmaq + depract*0.5 (amortización tintorería) mes-por-mes.

    Aproximación: usa la cuota mensual ACTUAL de cada activo (la columna
    `scintela.activos.cuota`) y la aplica a cada mes del rango. Asume que
    los activos existían y depreciaban con la misma cuota durante todo el
    rango — para periodos cortos (12 meses) suele ser razonable.

    Para el mes actual: prorratea por día (COEF = min(día, 30)/30), igual
    que la función `amortizaciones_mensuales()` de informes/queries.py.
    Para meses pasados: cuota completa (COEF = 1.0).
    """
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(tipo)) AS tipo,
               COALESCE(SUM(cuota), 0) AS total
          FROM scintela.activos
         WHERE COALESCE(cuota, 0) > 0
         GROUP BY 1
        """
    ) or []
    by = {r.get("tipo"): float(r.get("total") or 0) for r in rows}
    deprmaq = by.get("M", 0.0)
    depract = by.get("I", 0.0)
    dcc_full = deprmaq + depract * 0.5

    hoy = today_ec()
    res: dict = {}
    cur_yy, cur_mm = desde.year, desde.month
    end_yy, end_mm = hasta.year, hasta.month
    while (cur_yy, cur_mm) <= (end_yy, end_mm):
        if cur_yy == hoy.year and cur_mm == hoy.month:
            coef = min(hoy.day, 30) / 30.0
            res[(cur_yy, cur_mm)] = dcc_full * coef
        else:
            res[(cur_yy, cur_mm)] = dcc_full
        cur_mm += 1
        if cur_mm > 12:
            cur_mm = 1
            cur_yy += 1
    return res


def gs_produccion_tintoreria_por_mes(desde: date, hasta: date) -> dict:
    """Replica "Gs. Producción Tintorería" = `_gs_tin` de informes/queries.py
    mes-por-mes — IDÉNTICO al cálculo de Gastos del mes (TINTORERÍA TOTAL
    CON AMORT) para que las dos pantallas muestren el mismo número:
        = V4 + V5 + V6 (= xgast num 4/5/6  +  compras C/Q/T mapeadas)
        + amortización DCC (= deprmaq + depract * 0.5)

    TMT 2026-05-24 v3 — Bug detectado por dueña: la tabla mensual mostraba
    $241.238 en vez de $323.786 (que es lo que muestra Gastos del mes).
    Causa: el cálculo de Gastos del mes (gastos_xgast_v1_a_v9_mes) suma
    xgast + COMPRAS mapeadas a num 4/5/6 vía _SQL_COMPRA_NUM_CASE (tipo
    C/Q/T con concepto = sueldos/servicios/otros). Acá solo sumaba xgast
    y faltaban las compras → faltaban $82.548 (la diferencia).
    Fix: importar el mismo CASE y agregarlo a la query mensual.

    Devuelve dict {(yy, mm): total_us}. Meses sin gastos devuelven el DCC
    (porque la amortización corre aunque no haya xgast).
    """
    # Import local para evitar circular y porque solo necesitamos el
    # snippet SQL (no la función completa).
    from modules.informes.queries import _SQL_COMPRA_NUM_CASE

    out: dict = {}

    # 1) xgast num 4/5/6 — Tamara 2026-07-22: V6 (num=6) vuelve a incluir los
    # químico-insumos (QUIMSERTEC/TOSAVA/ECUAPLAST/NC/QI = gasto de tintorería,
    # como los CC; NO son colorantes POLI/ALG, que se valúan aparte en el stock),
    # igual que la matriz de Gastos del mes (las dos pantallas siguen cuadrando).
    rows_xg = db.fetch_all(
        """
        SELECT EXTRACT(YEAR  FROM fecha)::int  AS yy,
               EXTRACT(MONTH FROM fecha)::int  AS mm,
               COALESCE(SUM(importe), 0)::float AS total
          FROM scintela.xgast
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
           AND COALESCE(num, 0) IN (4, 5, 6)
           -- Excluir el wrapper manual 'QUIMICOS <prov>' (copia redundante del
           -- químico ya cargado como gasto bancario), igual que la matriz de V6.
           AND NOT (COALESCE(num, 0) = 6
                    AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'QUIMICOS %%')
         GROUP BY yy, mm
        """,
        (desde, hasta),
    )
    for r in rows_xg:
        out[(int(r["yy"]), int(r["mm"]))] = float(r["total"] or 0)

    # 2) compras mapeadas a num 4/5/6 vía el mismo CASE que usa el live.
    sql_cp = f"""
        SELECT EXTRACT(YEAR  FROM c.fecha)::int  AS yy,
               EXTRACT(MONTH FROM c.fecha)::int  AS mm,
               COALESCE(SUM(c.importe), 0)::float AS total
          FROM scintela.compra c
         WHERE c.fecha BETWEEN %s AND %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           -- Tamara 2026-07-22: excluir compras del puente formulas (colorantes/
           -- químicos material, valuados por stock) — no son gasto de tintorería.
           AND COALESCE(c.usuario_crea, '') NOT LIKE 'formulas%%'
           AND ({_SQL_COMPRA_NUM_CASE}) IN (4, 5, 6)
         GROUP BY yy, mm
    """
    rows_cp = db.fetch_all(sql_cp, (desde, hasta)) or []
    for r in rows_cp:
        k = (int(r["yy"]), int(r["mm"]))
        out[k] = out.get(k, 0.0) + float(r["total"] or 0)

    # 3) amortización DCC por mes (= deprmaq + depract*0.5)
    try:
        dcc = _amortizacion_dcc_por_mes(desde, hasta)
        for k, v in dcc.items():
            out[k] = out.get(k, 0.0) + v
    except Exception:
        pass

    return out


def tinto_filas_mes(yy: int, mm: int) -> list[dict]:
    """Filas crudas de scintela.tinto del mes (yy, mm) para la planilla
    de carga (/informes/tinto-carga). Incluye TODO menos stat X/Y para
    que la dueña vea exactamente lo que suma el balance."""
    from modules.informes.queries import CORTE_TINTURA  # corte dBase->formulas

    rows = db.fetch_all(
        """
        SELECT id_tinto, fecha, cod, color, kg, kgn, importe, stat,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.tinto
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND fecha < %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
         ORDER BY fecha DESC, id_tinto DESC
        """,
        (yy, mm, CORTE_TINTURA),
    ) or []
    rows = [dict(r) for r in rows]

    # CORTE tintura: del corte en adelante las filas salen de formulas_app.
    # Read-only: usuario_crea='formulas_app' -> el template no muestra "borrar".
    try:
        import calendar

        m_ini = date(yy, mm, 1)
        m_fin = date(yy, mm, calendar.monthrange(yy, mm)[1])
        f_desde = max(CORTE_TINTURA, m_ini)
        if f_desde <= m_fin:
            from modules.tintura import service as _tint_svc

            form = [
                {
                    "id_tinto": None,
                    "fecha": o.fecha,
                    "cod": o.cod,
                    "color": o.color,
                    "kg": o.kg,
                    "kgn": o.kgn,
                    "importe": o.importe,
                    "stat": "A",
                    "usuario_crea": "formulas_app",
                }
                for o in _tint_svc.tinto_equiv_formulas(
                    f_desde, m_fin, excluir_lavados=False
                )
            ]
            form.sort(key=lambda d: (d["fecha"] or date.min), reverse=True)
            rows = form + rows  # formulas (>=corte, más nuevas) arriba
    except Exception:  # noqa: BLE001 -- fail-soft
        pass

    return rows


def tinto_costos_catalogo() -> list[dict]:
    """Catálogo cod → color + costo $/kg (réplica PC del COSTOS.DBF)."""
    return db.fetch_all(
        """
        SELECT cod, color, costo
          FROM scintela.tinto_costos
         ORDER BY cod
        """
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


def tinto_formulas_bajos_fuertes_por_mes(
    desde: date, hasta: date, limite_bajos: float = 0.4
) -> list[dict]:
    """Igual que `tinto_bajos_fuertes_por_mes` pero leyendo de formulas_app.

    Los meses anteriores al CORTE_TINTURA (y en general los que no están en
    scintela.tinto, que sólo guarda el mes en curso del dBase) tienen su
    tinturado registrado en formulas_app, no en el dBase. Esta función trae
    esas órdenes vía `modules.tintura.service.tinto_equiv_formulas` (el mismo
    bridge que usa /informes/flujo-produccion del corte en adelante) y las
    clasifica Bajos/Fuertes con la MISMA regla ($/kg <= limite_bajos), para
    no duplicar lógica.

    Mapeo por orden (ver TintoEquivOrden):
        kg      = tela_cruda_kg   (bruto → denominador del $/kg)
        kgn     = tela_terminada_kg (neto → kg tinturados que se suman)
        importe = costo colorantes+auxiliares consumidos

    Sólo cuenta órdenes que ya salieron terminadas (kgn > 0), igual que el
    dBase sólo registra lotes cerrados, para que el $/kg no se diluya con
    órdenes en proceso. Excluye lavados (excluir_lavados=True en el bridge).

    Devuelve una fila por (yy, mm, tipo) con kg (= Σ kgn) y importe, en el
    MISMO shape que `tinto_bajos_fuertes_por_mes` para que el caller las
    pueda mezclar transparentemente.

    Fail-soft: si formulas_app no está disponible o rompe, devuelve [].
    """
    try:
        from modules.tintura import service as _tint_svc
    except Exception:  # noqa: BLE001
        return []

    try:
        ordenes = _tint_svc.tinto_equiv_formulas(desde, hasta) or []
    except Exception:  # noqa: BLE001
        return []

    # {(yy, mm, tipo): [kg_acum, imp_acum]}
    acc: dict[tuple, list] = {}
    for o in ordenes:
        kgn = float(o.kgn or 0.0)
        if kgn <= 0:
            continue  # todavía no salió terminada
        if o.fecha is None:
            continue
        imp = float(o.importe or 0.0)
        kg_bruto = float(o.kg or 0.0)
        den = kg_bruto if kg_bruto > 0 else kgn  # bruto preferido, fallback neto
        tipo = "Bajos" if (den > 0 and (imp / den) <= limite_bajos) else "Fuertes"
        k = (o.fecha.year, o.fecha.month, tipo)
        slot = acc.setdefault(k, [0.0, 0.0])
        slot[0] += kgn
        slot[1] += imp

    return [
        {"yy": yy, "mm": mm, "tipo": tipo, "kg": v[0], "importe": v[1]}
        for (yy, mm, tipo), v in sorted(acc.items())
    ]


def tinto_formulas_terminadas_por_mes(
    desde: date, hasta: date, limite_bajos: float = 0.4
) -> list[dict]:
    """Bajos/Fuertes por mes desde formulas_app — universo PRODUCCIÓN
    TINTORERÍA (dueña 2026-07-21: la tabla COSTOS DE TINTORERÍA y la página
    Producción Tintorería tienen que decir LO MISMO, garantizado por
    construcción).

    Universo = tinturado_resumen(terminado_desde/hasta) — la MISMA función
    que usa /informes/comparativa-tintoreria: órdenes con fecha_terminado en
    el rango, kg = tela_terminada_kg de la orden, agrupadas por el MES de
    fecha_terminado. Incluye lavados y reprocesos (están terminados y suman
    kg en Producción Tintorería; excluirlos era el gap 165k vs 216k). El
    Total kg del mes = Σ tela_terminada_kg de tinturado_resumen, exacto.

    Clasificación por orden, misma regla de siempre: $/kg = costo de
    colorantes+auxiliares c/IVA (costo_por_orden, mismo bridge y misma
    valuación que la página) sobre los kg de la orden (crudo preferido,
    fallback terminado) <= limite_bajos → Bajos. Órdenes terminadas sin
    colorante ($/kg = 0, p.ej. lavados) → Bajos; una orden con costo pero
    sin ningún kg (anomalía) → Fuertes, para no esconder el $.

    Mismo shape de salida que `tinto_bajos_fuertes_por_mes` (una fila por
    (yy, mm, tipo) con kg e importe). Fail-soft: [] si formulas no está.

    Reemplaza a `tinto_formulas_bajos_fuertes_por_mes` (por fecha de
    CREACIÓN, sin lavados, solo kgn>0) en la tabla mensual del flujo; esa
    queda para otros usos/historial.
    """
    try:
        from modules.tintura import service as _tint_svc

        ordenes = _tint_svc.tinturado_resumen(
            limite=20000, terminado_desde=desde, terminado_hasta=hasta,
        ) or []
        costos = _tint_svc.costo_por_orden(
            terminado_desde=desde, terminado_hasta=hasta,
        ) or {}
    except Exception:  # noqa: BLE001 -- fail-soft
        return []
    if not ordenes:
        return []

    acc2: dict[tuple, list] = {}
    for o in ordenes:
        ft = o.fecha_terminado
        if ft is None:
            continue  # tinturado_resumen con terminado_* no debería traerlas
        term = float(o.tela_terminada_kg or 0.0)
        cruda = float(o.tela_cruda_kg or 0.0)
        imp = float(costos.get(o.numero, 0.0) or 0.0)
        den = cruda if cruda > 0 else term
        if den > 0:
            tipo = "Bajos" if (imp / den) <= limite_bajos else "Fuertes"
        else:
            tipo = "Bajos" if imp <= 0 else "Fuertes"
        k = (ft.year, ft.month, tipo)
        slot = acc2.setdefault(k, [0.0, 0.0])
        slot[0] += term
        slot[1] += imp

    return [
        {"yy": yy, "mm": mm, "tipo": tipo, "kg": v[0], "importe": v[1]}
        for (yy, mm, tipo), v in sorted(acc2.items())
    ]
