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


def avance_esperado(desde: date, hasta: date, hoy: date) -> float:
    """Qué fracción del período ya transcurrió (0..1) — el 'ritmo'.

    Sirve para decirle "vas arriba/abajo del ritmo" en vez de sólo "71%",
    que sin contexto no dice nada el día 5 del mes.
    """
    total = _dias(desde, hasta)
    if total <= 0:
        return 1.0
    corridos = _dias(desde, min(hoy, hasta))
    return max(0.0, min(1.0, corridos / total))


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


def mis_clientes(vend: str) -> list[dict]:
    """Clientes del vendedor CON saldo vivo, con su vencido.

    Mismo criterio de saldo que `informes.queries.estado_cuenta_clientes_saldos`
    — si divergen, el vendedor y la dueña discuten sobre números distintos.
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


def ventas(vend: str, desde: date, hasta: date) -> float:
    """Facturado del período a los clientes del vendedor (sin anuladas)."""
    row = db.fetch_one(
        f"""
        SELECT COALESCE(SUM(f.importe), 0) AS total
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

    Misma definición que el módulo de comisiones (`cobranzas_detalle`):
    cheques que llegaron a banco + cobros no-cheque (efectivo, transferencia,
    depósito directo). Si las dos pantallas contaran distinto, el vendedor
    vería una comisión que no cierra con lo que ve acá.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE((
            SELECT SUM(ch.importe)
              FROM scintela.cheque ch
              JOIN scintela.cliente c ON c.codigo_cli = ch.codigo_cli
             WHERE UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))
               AND ch.fechad BETWEEN %(desde)s AND %(hasta)s
               AND ch.stat IN ('B','V','W','I','J','K','A')
        ), 0) + COALESCE((
            SELECT SUM(co.valor)
              FROM scintela.cobro co
              JOIN scintela.cliente c ON c.codigo_cli = co.codigo_cli
             WHERE UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))
               AND co.fecha BETWEEN %(desde)s AND %(hasta)s
               AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
        ), 0) AS total
        """,
        {"vend": vend, "desde": desde, "hasta": hasta},
    )
    return float((row or {}).get("total") or 0)


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


def ventas_por_semana(vend: str, desde: date, hasta: date) -> list[dict]:
    """Facturado agrupado por semana del período — las barritas del Inicio."""
    filas = db.fetch_all(
        f"""
        SELECT date_trunc('week', f.fecha)::date AS semana,
               COALESCE(SUM(f.importe), 0)       AS total
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
# Metas
# ---------------------------------------------------------------------------


def meta_mes(vend: str, anio: int, mes: int) -> float | None:
    """Meta cargada para ese mes. None = sin meta (la pantalla la omite)."""
    try:
        row = db.fetch_one(
            """
            SELECT monto FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s))
               AND anio = %s AND mes = %s
            """,
            (vend, int(anio), int(mes)),
        )
    except Exception:  # noqa: BLE001 — tabla todavía no creada (migración 0154)
        return None
    if not row or row.get("monto") is None:
        return None
    return float(row["monto"])


def meses_con_meta(vend: str, anio: int) -> list[int]:
    """Meses del año que tienen una meta cargada. [] si ninguno."""
    try:
        filas = db.fetch_all(
            """
            SELECT mes FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s)) AND anio = %s
               AND COALESCE(monto, 0) <> 0
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
        row = db.fetch_one(
            """
            SELECT COALESCE(SUM(monto), 0) AS total, COUNT(*) AS n
              FROM scintela.vendedor_meta
             WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s)) AND anio = %s
               AND COALESCE(monto, 0) <> 0
            """,
            (vend, int(anio)),
        )
    except Exception:  # noqa: BLE001
        return None
    if not row or not row.get("n"):
        return None
    return float(row["total"] or 0)


def ventas_en_meses(vend: str, anio: int, meses: list[int]) -> float:
    """Facturado del año restringido a ciertos MESES.

    Es la mitad que faltaba para que el anillo del año signifique algo con la
    meta a medio cargar: se compara lo vendido en los meses que TIENEN meta
    contra la suma de esas metas.
    """
    if not meses:
        return 0.0
    return float(
        (db.fetch_one(
            f"""
            SELECT COALESCE(SUM(f.importe), 0) AS total
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
    """Meta del período elegido.

    La dueña carga UNA meta por mes. La del año se suma; la de la semana se
    prorratea (meta del mes × 7 / días del mes) — no se carga aparte, para no
    pedirle 52 números por vendedor por año.
    """
    if periodo == "anio":
        # Ver el aviso de `meta_anio`: la vista tiene que comparar contra
        # `ventas_en_meses(meses_con_meta(...))`, no contra el año entero.
        return meta_anio(vend, hoy.year)
    m = meta_mes(vend, hoy.year, hoy.month)
    if m is None:
        return None
    if periodo == "semana":
        dias_mes = calendar.monthrange(hoy.year, hoy.month)[1]
        return m * 7.0 / dias_mes
    return m


def metas_del_anio(anio: int) -> list[dict]:
    """Grilla vendedor × mes para la pantalla de carga de la dueña."""
    try:
        return db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo)) AS codigo, mes, monto
              FROM scintela.vendedor_meta
             WHERE anio = %s
            """,
            (int(anio),),
        ) or []
    except Exception:  # noqa: BLE001
        return []


def guardar_meta(codigo: str, anio: int, mes: int, monto, usuario: str = "web") -> None:
    """Alta/edición de una meta. `monto` None o '' borra la fila."""
    cod = (codigo or "").strip().upper()[:3]
    if not cod:
        raise ValueError("Falta el código de vendedor.")
    if not (1 <= int(mes) <= 12):
        raise ValueError("Mes fuera de rango.")
    if monto in (None, ""):
        db.execute(
            """
            DELETE FROM scintela.vendedor_meta
             WHERE codigo = %s AND anio = %s AND mes = %s
            """,
            (cod, int(anio), int(mes)),
        )
        return
    db.execute(
        """
        INSERT INTO scintela.vendedor_meta
            (codigo, anio, mes, monto, usuario_actualiza, fecha_actualiza)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (codigo, anio, mes) DO UPDATE
           SET monto             = EXCLUDED.monto,
               usuario_actualiza = EXCLUDED.usuario_actualiza,
               fecha_actualiza   = CURRENT_TIMESTAMP
        """,
        (cod, int(anio), int(mes), monto, (usuario or "web")[:30]),
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
            "banco": (str(c.get("banco") or "").strip() or None),
            "es_cheque": (str(c.get("origen") or "").upper() == "CHE"),
            "importe": imp,
        })
    salida = sorted(grupos.values(), key=lambda g: g["cobrado"], reverse=True)
    for g in salida:
        g["comision"] = round(g["cobrado"] * pct / 100.0, 2)
        g["cobros"].sort(key=lambda c: (c["fecha"] is None, c["fecha"]))
    return salida


def comision_meses(vend: str, anio: int, hasta_mes: int) -> list[dict]:
    """Comisión mes a mes del año, de la más nueva a la más vieja."""
    out = []
    for mes in range(hasta_mes, 0, -1):
        ultimo = calendar.monthrange(anio, mes)[1]
        out.append(
            {
                "anio": anio,
                "mes": mes,
                "monto": comision(vend, date(anio, mes, 1), date(anio, mes, ultimo)),
            }
        )
    return out
