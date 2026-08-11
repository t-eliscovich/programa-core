"""Consultas del portal de vendedores — TODAS acotadas por código de vendedor.

TMT 2026-08-03 (dueña): "tienen que tener acceso solo a sus clientes, sus
facturas, sus cheques".

REGLA DE ESTE MÓDULO: ninguna función acepta un `codigo_cli` sin recibir
también el `vend` del usuario logueado. El scope no es un filtro opcional que
la vista puede olvidarse de pasar — es un parámetro obligatorio de cada
consulta. Si mañana alguien agrega una función acá y se olvida, no compila la
llamada.

El predicado de pertenencia es el mismo que usa el módulo de comisiones desde
2026-05-18, para que "los clientes de PPR" signifique exactamente lo mismo en
las dos pantallas:

    UPPER(TRIM(COALESCE(cliente.vend, ''))) = UPPER(TRIM(%(vend)s))
"""

from __future__ import annotations

import calendar
import time
from datetime import date, timedelta

import db

# Predicado canónico de pertenencia cliente→vendedor. Se interpola en las
# queries de abajo; el valor SIEMPRE va como parámetro %(vend)s.
_ES_MI_CLIENTE = "UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))"

# Criterio canónico de factura VIVA (idéntico al de cartera / estado de
# cuenta: saldo <> 0, stat vivo, sin el backfill de Asinfo).
_FACTURA_VIVA = """
      COALESCE(f.saldo, 0) <> 0
  AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
  AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
"""


# ---------------------------------------------------------------------------
# Períodos
# ---------------------------------------------------------------------------


def rango_periodo(periodo: str, hoy: date) -> tuple[date, date, str]:
    """(desde, hasta, etiqueta) para 'semana' | 'mes' | 'anio'.

    Semana = lunes a domingo de la semana de `hoy` (el vendedor piensa la
    semana comercial, no los últimos 7 días).
    """
    if periodo == "semana":
        desde = hoy - timedelta(days=hoy.weekday())
        return desde, desde + timedelta(days=6), "Esta semana"
    if periodo == "anio":
        return date(hoy.year, 1, 1), date(hoy.year, 12, 31), str(hoy.year)
    ultimo = calendar.monthrange(hoy.year, hoy.month)[1]
    return date(hoy.year, hoy.month, 1), date(hoy.year, hoy.month, ultimo), "Este mes"


def _dias(desde: date, hasta: date) -> int:
    return (hasta - desde).days + 1


def dias_habiles(desde: date, hasta: date) -> int:
    """Días LUNES A VIERNES del rango, ambos inclusive.

    ⭐ El ritmo se mide en días hábiles, no en días de calendario, porque la
    fábrica no factura los fines de semana. Medido sobre producción
    (facturas desde el 2026-02-01, por día ISO de la semana):

        lun 2395 · mar 2439 · mié 2446 · jue 2522 · vie 2027
        sáb 273 (1,2% del valor) · dom 0

    El domingo no existe: no hay UNA factura. Contarlos como días de venta
    no es una aproximación, es meter ceros a propósito en el divisor.

    Bug que destapó esto (dueña, 2026-08-04): *"siendo 4 de agosto ¿cómo es
    que en semana estoy arriba del ritmo y en mes abajo?"*. Agosto 2026
    arranca sábado: al 4 de agosto el MES ya había "consumido" 4 de 31 días
    (12,9%) mientras la SEMANA —que arranca el lunes 3— sólo 2 de 7 (28,6%
    pero sobre una meta de 7/31). Los mismos $750,34 daban arriba en un lado
    y abajo en el otro. Con días hábiles el sábado y el domingo dejan de
    empujar el mes, y las dos pantallas dan el mismo delta por construcción:
    al 04/08 el esperado es 2/21 de la meta mensual, se mire desde donde se
    mire.
    """
    total = 0
    d = desde
    while d <= hasta:
        if d.weekday() < 5:
            total += 1
        d += timedelta(days=1)
    return total


def avance_esperado(desde: date, hasta: date, hoy: date) -> float:
    """Qué fracción del período ya transcurrió (0..1) — el 'ritmo'.

    Sirve para decirle "vas arriba/abajo del ritmo" en vez de sólo "71%",
    que sin contexto no dice nada el día 5 del mes.

    Se cuenta en días HÁBILES — ver `dias_habiles()` para el por qué y para
    el bug que lo motivó.
    """
    total = dias_habiles(desde, hasta)
    if total <= 0:
        return 1.0
    corridos = dias_habiles(desde, min(hoy, hasta))
    return max(0.0, min(1.0, corridos / total))


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


def mis_clientes(vend: str) -> list[dict]:
    """Clientes del vendedor CON saldo vivo, con su vencido.

    Mismo criterio de saldo que `informes.queries.estado_cuenta_clientes_saldos`
    — si divergen, el vendedor y la dueña discuten sobre números distintos.

    ⚠ El ORDER BY de acá (más vencido primero) NO es el orden de ninguna
    pantalla: cada una pide el suyo — la lista de clientes por nombre, la hoja
    impresa por código, las alertas del Inicio por vencido. Es sólo un orden
    determinístico para que la lista no salga distinta en dos cargas.
    """
    return db.fetch_all(
        f"""
        SELECT c.codigo_cli,
               COALESCE(NULLIF(TRIM(c.nombre), ''), c.codigo_cli) AS nombre,
               COALESCE(NULLIF(TRIM(c.provincia), ''), '')        AS provincia,
               COALESCE(SUM(f.saldo), 0)                          AS saldo,
               COALESCE(SUM(CASE WHEN COALESCE(f.vencimiento, f.fecha)
                                      < CURRENT_DATE
                                 THEN f.saldo ELSE 0 END), 0)     AS vencido,
               MIN(CASE WHEN COALESCE(f.vencimiento, f.fecha) < CURRENT_DATE
                        THEN COALESCE(f.vencimiento, f.fecha) END) AS vence_mas_viejo,
               COUNT(*)                                           AS n_facturas
          FROM scintela.factura f
          JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE {_ES_MI_CLIENTE}
           AND {_FACTURA_VIVA}
         GROUP BY c.codigo_cli, c.nombre, c.provincia
        HAVING COALESCE(SUM(f.saldo), 0) <> 0
         ORDER BY COALESCE(SUM(CASE WHEN COALESCE(f.vencimiento, f.fecha)
                                         < CURRENT_DATE
                                    THEN f.saldo ELSE 0 END), 0) DESC,
                  COALESCE(SUM(f.saldo), 0) DESC
        """,
        {"vend": vend},
    ) or []


def cliente_es_mio(vend: str, codigo_cli: str) -> bool:
    """Guard de pertenencia. Se llama ANTES de mostrar cualquier ficha.

    Sin esto, `/mi-cartera/cliente/<codigo>` sería una fuga directa: alcanzaría
    con tipear el código de un cliente ajeno en la barra de direcciones.
    """
    if not vend or not codigo_cli:
        return False
    return bool(
        db.fetch_one(
            f"""
            SELECT 1 AS ok
              FROM scintela.cliente c
             WHERE c.codigo_cli = %(cod)s
               AND {_ES_MI_CLIENTE}
            """,
            {"cod": codigo_cli, "vend": vend},
        )
    )


def vendedores_activos() -> list[dict]:
    """Catálogo para la pantalla de metas de la dueña."""
    return db.fetch_all(
        """
        SELECT codigo, COALESCE(NULLIF(TRIM(nombre), ''), codigo) AS nombre
          FROM scintela.vendedor
         WHERE COALESCE(activo, TRUE) = TRUE
         ORDER BY codigo
        """
    ) or []


def nombre_vendedor(vend: str) -> str:
    row = db.fetch_one(
        """
        SELECT COALESCE(NULLIF(TRIM(nombre), ''), codigo) AS nombre
          FROM scintela.vendedor
         WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s))
        """,
        (vend,),
    )
    return (row or {}).get("nombre") or vend


# ---------------------------------------------------------------------------
# Números del Inicio
# ---------------------------------------------------------------------------


def ventas_kg(vend: str, desde: date, hasta: date) -> float:
    """KILOS facturados del período a los clientes del vendedor (sin anuladas).

    ⭐ La venta del vendedor se mide en KILOS, no en dólares — dueña
    2026-08-05: *"En las ventas de mes por favor poner los kilos, las metas se
    mide en kilos"*. Es la unidad en la que se negocia (el precio es por
    kilo), en la que se planifica la producción y en la que la dueña le pone
    la meta a cada uno. Medida en dólares, "vendió 10% más" mezcla dos
    noticias distintas: vendió más tela, o vendió la misma tela más cara.

    El nombre dice `_kg` a propósito. La función se llamaba `ventas()` y
    devolvía plata; si sólo se le cambiaba el cuerpo, cualquiera que la leyera
    el mes que viene seguiría entendiendo dólares y el error no daría ningún
    síntoma — los dos son números que se suman igual.

    `factura.kg` está cargado: sobre las ~1.500 facturas de los 6 vendedores
    entre junio y agosto de 2026 hay 21 sin kg (1,4%), todas de importes
    chicos. No se excluyen — suman 0 kg, que es exactamente lo que el dato
    dice.
    """
    row = db.fetch_one(
        f"""
        SELECT COALESCE(SUM(f.kg), 0) AS total
          FROM scintela.factura f
          JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE {_ES_MI_CLIENTE}
           AND f.fecha BETWEEN %(desde)s AND %(hasta)s
           AND (f.stat IS NULL OR f.stat <> 'X')
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
        """,
        {"vend": vend, "desde": desde, "hasta": hasta},
    )
    return float((row or {}).get("total") or 0)


def cobrado(vend: str, desde: date, hasta: date) -> float:
    """Cobranza ACREDITADA del período.

    ⭐ TMT 2026-08-04 — esta función tenía su PROPIA copia de la consulta, con
    su propia lista de estados —`('B','V','W','I','J','K','A')`, **sin la
    'C'**— y sin la rama de caja. O sea: el KPI de arriba de la pantalla del
    vendedor NO contaba los cobros de ventanilla, mientras que el detalle de
    abajo —que sí reusa `comisiones.cobranzas_detalle`— sí los contaba. El
    total no era la suma del desglose que la propia pantalla mostraba, que es
    justo el invariante que ya se había arreglado una vez.

    Ahora delega en `comisiones.queries.cobranza_periodo`: **una sola
    definición**. Copiar la consulta fue el error; que las dos vuelvan a
    divergir ahora exige editar un solo lugar.
    """
    from modules.comisiones import queries as comisiones_queries

    return comisiones_queries.cobranza_periodo(vend, desde, hasta)


def por_cobrar(vend: str) -> dict:
    """Saldo vivo total del vendedor y cuánto de eso ya está vencido.

    `n_clientes` cuenta los clientes cuyo saldo NETO es distinto de cero —
    el MISMO criterio que `mis_clientes()`. Con un COUNT(DISTINCT) plano
    sobre las facturas, un cliente cuyas facturas netean a cero (una NC que
    cancela una factura) sumaba acá y no aparecía en la lista: el Inicio
    decía 34 y la lista mostraba 33. Verificado en vivo con RMY.
    """
    row = db.fetch_one(
        f"""
        WITH por_cli AS (
            SELECT f.codigo_cli,
                   SUM(f.saldo) AS saldo,
                   SUM(CASE WHEN COALESCE(f.vencimiento, f.fecha) < CURRENT_DATE
                            THEN f.saldo ELSE 0 END) AS vencido
              FROM scintela.factura f
              JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
             WHERE {_ES_MI_CLIENTE}
               AND {_FACTURA_VIVA}
             GROUP BY f.codigo_cli
            HAVING COALESCE(SUM(f.saldo), 0) <> 0
        )
        SELECT COALESCE(SUM(saldo), 0)   AS saldo,
               COALESCE(SUM(vencido), 0) AS vencido,
               COUNT(*)                  AS n_clientes
          FROM por_cli
        """,
        {"vend": vend},
    )
    row = row or {}
    return {
        "saldo": float(row.get("saldo") or 0),
        "vencido": float(row.get("vencido") or 0),
        "n_clientes": int(row.get("n_clientes") or 0),
    }


def ventas_kg_por_semana(vend: str, desde: date, hasta: date) -> list[dict]:
    """KILOS facturados por semana del período — las barritas del Inicio.

    Misma unidad que el anillo de arriba (ver `ventas_kg`): si el anillo
    dijera kilos y las barras dólares, la barra más alta podría no ser la
    semana en que más se vendió.
    """
    filas = db.fetch_all(
        f"""
        SELECT date_trunc('week', f.fecha)::date AS semana,
               COALESCE(SUM(f.kg), 0)            AS total
          FROM scintela.factura f
          JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE {_ES_MI_CLIENTE}
           AND f.fecha BETWEEN %(desde)s AND %(hasta)s
           AND (f.stat IS NULL OR f.stat <> 'X')
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
         GROUP BY 1
         ORDER BY 1
        """,
        {"vend": vend, "desde": desde, "hasta": hasta},
    ) or []
    return [{"semana": f["semana"], "total": float(f["total"] or 0)} for f in filas]


# ---------------------------------------------------------------------------
# Metas — EN KILOS (migración 0163)
# ---------------------------------------------------------------------------
# La columna se llamaba `monto` y guardaba dólares; la 0163 la renombra a `kg`.
#
# ⚠ El deploy NO corre migraciones. Entre que este código llega al server y
# que alguien aprieta "Aplicar pendientes" en /admin/migraciones, la columna
# todavía se llama `monto`. Por eso el nombre se resuelve en runtime en vez de
# ir hardcodeado: si no, la pantalla de metas quedaría rota justo en esa
# ventana — que es exactamente cuando nadie la está mirando.
#
# ⭐ El positivo se cachea PARA SIEMPRE y el negativo 60 segundos. Una columna
# que existe no desaparece; una que no existe aparece en el momento en que la
# dueña corre la migración. Darles la misma vida es el bug del 2026-08-03 con
# `usuario.vend`: la migración se aplicó bien (exit 0) y la app siguió
# convencida de que la columna no estaba, sin una sola línea en los logs y con
# un único síntoma, un guardado que se rechazaba pidiendo correr una migración
# que ya estaba corrida.
_COL_META_KG: bool = False
_COL_META_CHEQUEADO_EN: float = 0.0
_COL_META_TTL_NEGATIVO = 60.0  # segundos


def _columna_meta() -> str:
    """`'kg'` si ya corrió la migración 0163; `'monto'` mientras tanto.

    El VALOR guardado son kilos en los dos casos — lo único que cambia es
    cómo se llama la columna. Las metas viejas, que sí eran dólares, las
    borra la propia migración.
    """
    global _COL_META_KG, _COL_META_CHEQUEADO_EN
    if _COL_META_KG:
        return "kg"
    ahora = time.monotonic()
    if (_COL_META_CHEQUEADO_EN
            and (ahora - _COL_META_CHEQUEADO_EN) < _COL_META_TTL_NEGATIVO):
        return "monto"
    _COL_META_CHEQUEADO_EN = ahora
    try:
        _COL_META_KG = bool(db.fetch_one(
            """
            SELECT 1 AS ok FROM information_schema.columns
             WHERE table_schema = 'scintela'
               AND table_name   = 'vendedor_meta'
               AND column_name  = 'kg'
            """
        ))
    except Exception:  # noqa: BLE001 — sin DB o sin permisos: asumimos que no
        _COL_META_KG = False
    return "kg" if _COL_META_KG else "monto"


def _reset_cache_columna_meta() -> None:
    """Sólo para tests."""
    global _COL_META_KG, _COL_META_CHEQUEADO_EN
    _COL_META_KG = False
    _COL_META_CHEQUEADO_EN = 0.0


def meta_mes(vend: str, anio: int, mes: int) -> float | None:
    """Meta de KILOS cargada para ese mes. None = sin meta (no se dibuja el
    anillo: un 0% falso es peor que no mostrar nada)."""
    col = _columna_meta()
    try:
        row = db.fetch_one(
            f"""
            SELECT {col} AS kg FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s))
               AND anio = %s AND mes = %s
            """,
            (vend, int(anio), int(mes)),
        )
    except Exception:  # noqa: BLE001 — tabla todavía no creada (migración 0154)
        return None
    if not row or row.get("kg") is None:
        return None
    return float(row["kg"])


def meses_con_meta(vend: str, anio: int) -> list[int]:
    """Meses del año que tienen una meta cargada. [] si ninguno."""
    try:
        filas = db.fetch_all(
            f"""
            SELECT mes FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s)) AND anio = %s
               AND COALESCE({_columna_meta()}, 0) <> 0
             ORDER BY mes
            """,
            (vend, int(anio)),
        )
    except Exception:  # noqa: BLE001 — tabla todavía no creada (migración 0154)
        return []
    return [int(f["mes"]) for f in filas or []]


def meta_anio(vend: str, anio: int) -> float | None:
    """Meta del año = suma de las metas mensuales CARGADAS. None si no hay.

    ⚠ OJO AL COMPARARLA: es la suma de los meses que la dueña cargó, no una
    meta de 12 meses. Contra las ventas del año ENTERO da un disparate — el
    2026-08-03, con sólo agosto cargado ($10.000) y $334.524 vendidos en el
    año, el anillo mostraba **3345%**. Para comparar like con like está
    `ventas_en_meses()` + `meses_con_meta()`.
    """
    try:
        col = _columna_meta()
        row = db.fetch_one(
            f"""
            SELECT COALESCE(SUM({col}), 0) AS total, COUNT(*) AS n
              FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s)) AND anio = %s
               AND COALESCE({col}, 0) <> 0
            """,
            (vend, int(anio)),
        )
    except Exception:  # noqa: BLE001
        return None
    if not row or not row.get("n"):
        return None
    return float(row["total"] or 0)


def ventas_kg_en_meses(vend: str, anio: int, meses: list[int]) -> float:
    """KILOS facturados del año restringidos a ciertos MESES.

    Es la mitad que faltaba para que el anillo del año signifique algo con la
    meta a medio cargar: se compara lo vendido en los meses que TIENEN meta
    contra la suma de esas metas.
    """
    if not meses:
        return 0.0
    return float(
        (db.fetch_one(
            f"""
            SELECT COALESCE(SUM(f.kg), 0) AS total
              FROM scintela.factura f
              JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
             WHERE {_ES_MI_CLIENTE}
               AND EXTRACT(YEAR FROM f.fecha) = %(anio)s
               AND EXTRACT(MONTH FROM f.fecha) = ANY(%(meses)s)
               AND (f.stat IS NULL OR f.stat <> 'X')
               AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
            """,
            {"vend": vend, "anio": int(anio), "meses": [int(m) for m in meses]},
        ) or {}).get("total") or 0
    )


def meta_periodo(vend: str, periodo: str, hoy: date) -> float | None:
    """Meta del período elegido, en KILOS.

    La dueña carga UNA meta por mes. La del año se suma; la de la semana se
    prorratea — no se carga aparte, para no pedirle 52 números por vendedor
    por año.

    El prorrateo semanal va por días HÁBILES (meta del mes × hábiles de la
    semana / hábiles del mes), no por 7/31. Esto no es cosmética: hace que
    la plata esperada POR DÍA HÁBIL sea idéntica en las dos pantallas, y por
    lo tanto que "arriba/abajo del ritmo" no pueda decir cosas opuestas en
    la misma fecha. Con 7/31 sí podía — pasó el 2026-08-04.
    """
    if periodo == "anio":
        # Ver el aviso de `meta_anio`: la vista tiene que comparar contra
        # `ventas_en_meses(meses_con_meta(...))`, no contra el año entero.
        return meta_anio(vend, hoy.year)
    m = meta_mes(vend, hoy.year, hoy.month)
    if m is None:
        return None
    if periodo == "semana":
        d_mes, h_mes, _ = rango_periodo("mes", hoy)
        habiles_mes = dias_habiles(d_mes, h_mes)
        if habiles_mes <= 0:  # pragma: no cover — ningún mes real da 0
            return m * 7.0 / calendar.monthrange(hoy.year, hoy.month)[1]
        d_sem, h_sem, _ = rango_periodo("semana", hoy)
        return m * dias_habiles(d_sem, h_sem) / habiles_mes
    return m


def ventas_kg_por_vendedor_mes(anio: int) -> dict[tuple[str, int], float]:
    """KILOS facturados por vendedor y mes del año — para la grilla de metas.

    Pedido de Andrés 2026-08-05: *"en la parte donde tenemos las metas le
    pongas los kilos vendidos reales y el % de cumplimiento de las ventas de
    cada vendedor"*. Una meta sin el real al lado es un número que nadie
    vuelve a mirar.

    ⚡ UNA query para los 6 vendedores × 12 meses. Llamar a `ventas_kg()` en
    un loop serían 72 consultas por carga de pantalla — el mismo error que ya
    costó 3.190 ms en la pantalla de comisión.

    Mismo criterio que `ventas_kg()` (sin anuladas, sin el backfill de
    Asinfo): si la grilla de la dueña y el portal del vendedor contaran
    distinto, la conversación del mes siguiente es imposible.
    """
    filas = db.fetch_all(
        """
        SELECT UPPER(TRIM(COALESCE(c.vend, ''))) AS vend,
               EXTRACT(MONTH FROM f.fecha)::int  AS mes,
               COALESCE(SUM(f.kg), 0)            AS kg
          FROM scintela.factura f
          JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE EXTRACT(YEAR FROM f.fecha) = %(anio)s
           AND (f.stat IS NULL OR f.stat <> 'X')
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
           AND COALESCE(c.vend, '') <> ''
         GROUP BY 1, 2
        """,
        {"anio": int(anio)},
    ) or []
    return {(f["vend"], int(f["mes"])): float(f["kg"] or 0) for f in filas}


def metas_del_anio(anio: int) -> list[dict]:
    """Grilla vendedor × mes para la pantalla de carga de la dueña."""
    try:
        return db.fetch_all(
            f"""
            SELECT UPPER(TRIM(codigo)) AS codigo, mes, {_columna_meta()} AS kg
              FROM scintela.vendedor_meta
             WHERE anio = %s
            """,
            (int(anio),),
        ) or []
    except Exception:  # noqa: BLE001
        return []


def guardar_meta(codigo: str, anio: int, mes: int, kg, usuario: str = "web") -> None:
    """Alta/edición de una meta EN KILOS. `kg` None o '' borra la fila."""
    cod = (codigo or "").strip().upper()[:3]
    if not cod:
        raise ValueError("Falta el código de vendedor.")
    if not (1 <= int(mes) <= 12):
        raise ValueError("Mes fuera de rango.")
    if kg in (None, ""):
        db.execute(
            """
            DELETE FROM scintela.vendedor_meta
             WHERE codigo = %s AND anio = %s AND mes = %s
            """,
            (cod, int(anio), int(mes)),
        )
        return
    col = _columna_meta()
    db.execute(
        f"""
        INSERT INTO scintela.vendedor_meta
            (codigo, anio, mes, {col}, usuario_actualiza, fecha_actualiza)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (codigo, anio, mes) DO UPDATE
           SET {col}             = EXCLUDED.{col},
               usuario_actualiza = EXCLUDED.usuario_actualiza,
               fecha_actualiza   = CURRENT_TIMESTAMP
        """,
        (cod, int(anio), int(mes), kg, (usuario or "web")[:30]),
    )


# ---------------------------------------------------------------------------
# Comisión
# ---------------------------------------------------------------------------


def _pct_comision(vend: str) -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(pct_comision, 0) AS pct
          FROM scintela.vendedor
         WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s))
        """,
        (vend,),
    )
    return float((row or {}).get("pct") or 0)


def comision(vend: str, desde: date, hasta: date) -> float:
    """Monto de comisión del período.

    Devuelve SÓLO el monto, nunca el porcentaje ni la base — decisión de la
    dueña 2026-08-03: el vendedor no ve su %. (Mostrar monto Y base deja
    despejar el % en dos segundos, así que la base tampoco sale.)
    """
    return round(cobrado(vend, desde, hasta) * _pct_comision(vend) / 100.0, 2)


def cobros_del_mes(vend: str, anio: int, mes: int) -> list[dict]:
    """Los cobros ACREDITADOS del mes, uno por uno, con su cliente.

    Es el MISMO detalle que usa la pantalla de comisiones de la oficina
    (`modules.comisiones.queries.cobranzas_detalle`): cheques que llegaron a
    banco + cobros no-cheque de `scintela.cobro`. Se reusa a propósito — si
    el vendedor y la dueña vieran dos listas distintas del mismo mes, la
    conversación siguiente es imposible.
    """
    from modules.comisiones import queries as comisiones_queries

    return comisiones_queries.cobranzas_detalle(vend, anio=int(anio), mes=int(mes))


def comision_por_cliente(vend: str, anio: int, mes: int) -> list[dict]:
    """Los cobros del mes AGRUPADOS por cliente, con la comisión de cada uno.

    TMT 2026-08-03 (dueña): *"seguro quieren saber de qué clientes están
    ganando esta comisión, que la comisión diga de qué cobranza es"*. Sin
    esto, la pantalla mostraba un número sin forma de contestarse "¿de dónde
    salió?", que es la única pregunta que un vendedor le hace a su comisión.

    Cada grupo trae sus cobros uno por uno (fecha, documento, banco, importe),
    ordenados de mayor a menor cobrado.
    """
    pct = _pct_comision(vend)
    grupos: dict[str, dict] = {}
    for c in cobros_del_mes(vend, anio, mes):
        cod = (c.get("codigo_cli") or "").strip()
        g = grupos.setdefault(cod, {
            "codigo_cli": cod,
            "nombre": (c.get("cliente") or cod or "—").strip(),
            "cobrado": 0.0,
            "cobros": [],
        })
        imp = float(c.get("importe") or 0)
        g["cobrado"] += imp
        g["cobros"].append({
            "fecha": c.get("fecha"),
            "doc": (str(c.get("doc") or "").strip() or None),
            "id_origen": c.get("id_origen"),
            "banco": (str(c.get("banco") or "").strip() or None),
            "es_cheque": (str(c.get("origen") or "").upper() == "CHE"),
            "importe": imp,
        })
    salida = sorted(grupos.values(), key=lambda g: g["cobrado"], reverse=True)
    for g in salida:
        g["comision"] = round(g["cobrado"] * pct / 100.0, 2)
        g["cobros"].sort(key=lambda c: (c["fecha"] is None, c["fecha"]))
    return salida


def comision_mes(vend: str, anio: int, mes: int) -> float:
    """La comisión de un MES = la SUMA de lo que muestra el desglose.

    ⭐ No es `round(cobrado_total × pct)`. Redondear el total por un lado y
    cada cliente por el otro da resultados distintos: con la cartera de RMY
    en agosto, el encabezado decía **$7,73** y el detalle sumaba **$7,74**.
    Un centavo alcanza para que el vendedor deje de creerle a los dos
    números, y tiene razón — uno de los dos está mal.

    El detalle manda: es lo que se puede auditar cobro por cobro. El total
    es su suma, por definición.
    """
    return round(sum(g["comision"] for g in comision_por_cliente(vend, anio, mes)), 2)


def comision_meses(vend: str, anio: int, hasta_mes: int) -> list[dict]:
    """Comisión mes a mes del año, de la más nueva a la más vieja.

    ⚡ UNA sola query para todo el año. Antes llamaba a `comision_mes()` en
    un loop, o sea el detalle completo (UNION cheque+cobro con JOIN a
    cliente) una vez por mes: 8 veces por carga de pantalla. La pantalla
    tardaba **3.190 ms** contra 162 ms del Inicio — dueña 2026-08-04:
    *"también está súper lento"*.

    La cuenta es la MISMA, no una aproximación rápida: se redondea la
    comisión de cada cliente y recién ahí se suma el mes, igual que
    `comision_mes()`. Un test compara las dos, mes por mes, con igualdad
    exacta — si alguien "optimiza" esto sumando primero, se rompe.
    """
    from modules.comisiones import queries as comisiones_queries

    pct = _pct_comision(vend)
    por_mes: dict[int, float] = {}
    for f in comisiones_queries.cobranzas_por_cliente_anio(
        vend, anio=int(anio), hasta_mes=int(hasta_mes)
    ):
        mes = int(f["mes"])
        cobrado_cli = float(f["cobrado"] or 0)
        por_mes[mes] = por_mes.get(mes, 0.0) + round(cobrado_cli * pct / 100.0, 2)
    return [
        {"anio": anio, "mes": mes, "monto": round(por_mes.get(mes, 0.0), 2)}
        for mes in range(hasta_mes, 0, -1)
    ]
