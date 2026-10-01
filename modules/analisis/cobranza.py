"""Análisis de COBRANZA: quién paga, quién está atrasado, cada cliente contra sí mismo.

TMT 2026-09-30 (Tamara): *"quiero hacer un análisis de cobranza… quién paga,
quién está atrasado… un semáforo de clientes"*. Se exploró primero en una
página aparte (con los datos del 29/09) y se trajo acá para que esté siempre
al día. Sólo datos de Programa Core — decisión de Tamara: *"yo quiero usar
programa core. no datos incompletos de asinfo"* (la cobranza que carga
contabilidad en Asinfo registra ~87% de lo que tiene PC y le faltan
transferencias).

La pantalla tiene tres ideas:

1. **Qué es normal** — los límites del semáforo salen de cómo se comportan
   TODOS los clientes (la mitad, 1 de cada 4, 1 de cada 10 los superan), no de números
   inventados.
2. **Semáforo** por reglas explicables (basta una condición).
3. **Puntaje de riesgo** 0–100 que ordena qué tan mal está cada uno contra el
   resto, para decidir a quién cobrar primero.

⚠ Medidas y trampas (todas pagadas en la exploración):

- **Días desde la FECHA de la factura, no desde el vencimiento.** El
  vencimiento a 90 días (mig 0169) hace que casi nada figure vencido.
- **Plazo de pago = hasta que el dinero está DISPONIBLE** (`fechad` del
  cheque), no hasta que se recibe: el que paga con cheque lo entrega a los ~40
  días con fecha a ~97.
- **"Tiene saldo y no compra" cuenta sólo si hay FACTURAS impagas.** Un
  cliente que sólo tiene cheques por cobrar ya pagó.
- **Los cupos no están al día** (69 clientes los superaban el 29/09): el
  semáforo NO los mira; se listan aparte para revisarlos.
- **El historial de vínculos cobro→factura empieza el 14/05/2026** y los
  pagos por mes, en julio. La comparación contra sí mismo crece mes a mes; la
  foto diaria de la cartera (`cartera_snapshots`, desde el 30/09) va a
  permitir más.
"""

from __future__ import annotations

from datetime import date

import db
from filters import today_ec

# ── Límites del semáforo (salen de "qué es normal", medido el 29/09/2026) ────
ROJO_DIAS_FACTURA = 150        # ~5% de los clientes llega
AMARILLO_DIAS_FACTURA = 100    # menos de 2 de cada 10
AMARILLO_VECES_PLAZO = 1.25    # 1 de cada 4 debe más de 1,2 veces lo suyo
AMARILLO_EMPEORO_DIAS = 15     # 9 de cada 10 cambian menos de 16 días
REBOTE_VIEJO_DIAS = 7   # Tamara 30/09: "en vez de 30 dias sea 7"
SALDO_MINIMO = 1000            # debajo de esto no se lista (salvo rebotes)
PAGADO_MINIMO_HISTORIA = 2000  # para confiar en el "plazo habitual"
# Tamara 01/10: "si son centavos me gustaria no contarlas". Una factura con
# menos de esto pendiente no cuenta como "factura impaga" (ni para el
# semáforo ni para la evolución): son restos de redondeo, no deuda.
CENTAVOS = 5

# Pesos del puntaje.
PESO_ANTIGUEDAD = 0.45
PESO_DEUDA_VS_PLAZO = 0.35
PESO_TENDENCIA = 0.20

COBRAR_PRIMERO_PUNTAJE = 70
COBRAR_PRIMERO_SALDO = 20000

_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# ── Lecturas ────────────────────────────────────────────────────────────────

_SQL_CLIENTES = """
WITH fac AS (
  SELECT f.codigo_cli, SUM(f.saldo) AS saldo_fac,
         MAX(%(hoy)s::date - f.fecha) FILTER (WHERE f.saldo >= %(centavos)s) AS edad_max
    FROM scintela.factura f
   WHERE (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
     AND COALESCE(f.usuario_crea,'') <> 'asinfo-backfill'
     AND COALESCE(f.saldo,0) <> 0
   GROUP BY 1),
ch AS (
  SELECT c.codigo_cli,
         SUM(c.importe) FILTER (WHERE UPPER(TRIM(COALESCE(c.stat,''))) IN ('Z','P','D')) AS ch_cartera,
         SUM(c.importe) FILTER (WHERE UPPER(TRIM(COALESCE(c.stat,''))) IN ('1','2','3','R','9')) AS ch_reb,
         COUNT(*) FILTER (WHERE UPPER(TRIM(COALESCE(c.stat,''))) IN ('1','2','3','R','9')) AS n_reb
    FROM scintela.cheque c
   WHERE c.codigo_cli IS NOT NULL GROUP BY 1),
reb AS (
  SELECT c.codigo_cli, COUNT(*) AS n_reb_hist
    FROM scintela.mov_doble m JOIN scintela.cheque c ON c.id_cheque = m.origen_id
   WHERE m.tipo = 'cheque_devuelto' AND m.estado = 'activo'
     AND m.fecha_creacion >= %(hoy)s::date - 365
   GROUP BY 1),
reb_edad AS (
  SELECT c.codigo_cli,
         MAX(%(hoy)s::date - COALESCE(
               (SELECT MAX(m.fecha_creacion)::date FROM scintela.mov_doble m
                 WHERE m.tipo = 'cheque_devuelto' AND m.estado = 'activo'
                   AND m.origen_id = c.id_cheque),
               c.fecha_recibido, c.fecha)) AS reb_dias
    FROM scintela.cheque c
   WHERE UPPER(TRIM(COALESCE(c.stat,''))) IN ('1','2','3','R','9')
     AND c.codigo_cli IS NOT NULL
   GROUP BY 1),
ven AS (
  SELECT f.codigo_cli, SUM(f.importe) FILTER (WHERE f.importe > 0) AS venta90
    FROM scintela.factura f
   WHERE f.fecha > %(hoy)s::date - 90 AND COALESCE(f.stat,'') <> 'X'
     AND COALESCE(f.tipo,'') <> 'ND'
     AND COALESCE(f.usuario_crea,'') <> 'asinfo-backfill'
   GROUP BY 1),
pag AS (
  SELECT x.codigo_cli, SUM(x.importe) AS pagado,
         SUM(x.importe * (COALESCE(c.fechad, c.fecha) - f.fecha)) / SUM(x.importe) AS dias_plata
    FROM scintela.chequesxfact x
    JOIN scintela.factura f ON f.id_factura = x.id_fact
    JOIN scintela.cheque c ON c.id_cheque = x.id_cheque
   WHERE f.fecha >= %(hoy)s::date - 180 AND x.importe > 0
     AND c.no_banco NOT IN (95, 97, 98)
   GROUP BY 1)
SELECT cl.codigo_cli AS cod, COALESCE(cl.nombre,'') AS nombre,
       COALESCE(NULLIF(TRIM(cl.vend),''), 'Casa') AS vend,
       COALESCE(cl.cupo,0) AS cupo, COALESCE(cl.stop,'') AS stop,
       COALESCE(fac.saldo_fac,0) AS fac, COALESCE(ch.ch_cartera,0) AS ch,
       COALESCE(ch.ch_reb,0) AS reb, COALESCE(ch.n_reb,0) AS n_reb,
       COALESCE(reb.n_reb_hist,0) AS n_reb_hist, reb_edad.reb_dias,
       fac.edad_max, COALESCE(ven.venta90,0) AS venta90,
       pag.pagado, ROUND(pag.dias_plata) AS dias_plata
  FROM scintela.cliente cl
  LEFT JOIN fac ON fac.codigo_cli = cl.codigo_cli
  LEFT JOIN ch  ON ch.codigo_cli  = cl.codigo_cli
  LEFT JOIN reb ON reb.codigo_cli = cl.codigo_cli
  LEFT JOIN reb_edad ON reb_edad.codigo_cli = cl.codigo_cli
  LEFT JOIN ven ON ven.codigo_cli = cl.codigo_cli
  LEFT JOIN pag ON pag.codigo_cli = cl.codigo_cli
 WHERE COALESCE(fac.saldo_fac,0) <> 0 OR COALESCE(ch.ch_cartera,0) <> 0
    OR COALESCE(ch.ch_reb,0) <> 0 OR COALESCE(ven.venta90,0) > 0
"""

# Plazo con que pagó cada cliente en cada mes (por fecha en que se RECIBIÓ el
# pago): días desde la factura hasta que el dinero estuvo disponible.
_SQL_MESES = """
SELECT x.codigo_cli AS cod,
       date_trunc('month', COALESCE(c.fecha_recibido, c.fecha))::date AS mes,
       SUM(x.importe) AS plata,
       SUM(x.importe * (COALESCE(c.fechad, c.fecha) - f.fecha)) / SUM(x.importe) AS dias
  FROM scintela.chequesxfact x
  JOIN scintela.factura f ON f.id_factura = x.id_fact
  JOIN scintela.cheque c ON c.id_cheque = x.id_cheque
 WHERE x.importe > 0 AND c.no_banco NOT IN (95, 97, 98)
   AND COALESCE(c.fecha_recibido, c.fecha) >= %(desde)s
   AND COALESCE(c.fecha_recibido, c.fecha) <= %(hoy)s
 GROUP BY 1, 2
"""

_SQL_MEDIOS = """
SELECT CASE WHEN c.no_banco IN (90, 91) THEN 'Depósito o transferencia'
            WHEN c.no_banco = 99 THEN 'Efectivo' ELSE 'Cheque' END AS medio,
       SUM(x.importe) AS plata,
       SUM(x.importe * (COALESCE(c.fecha_recibido, c.fecha) - f.fecha)) / SUM(x.importe) AS entrega,
       SUM(x.importe * (COALESCE(c.fechad, c.fecha) - f.fecha)) / SUM(x.importe) AS disponible
  FROM scintela.chequesxfact x
  JOIN scintela.factura f ON f.id_factura = x.id_fact
  JOIN scintela.cheque c ON c.id_cheque = x.id_cheque
 WHERE x.importe > 0 AND c.no_banco NOT IN (95, 97, 98)
   AND COALESCE(c.fecha_recibido, c.fecha) > %(hoy)s::date - 120
 GROUP BY 1 ORDER BY 2 DESC
"""


# ── Evolución mes a mes de cada cliente ─────────────────────────────────────
# Tamara 30/09: *"ver de ese cliente como evoluciona en el tiempo… si esta
# rojo vemos donde termino cada mes"*. No hay fotos viejas (la foto diaria
# arranca el 30/09), así que el saldo al cierre de cada mes se RECONSTRUYE
# para atrás: saldo de hoy de cada factura + lo que se le aplicó DESPUÉS del
# cierre (chequesxfact, que está completo desde junio). Por eso arranca en
# junio y no antes.
EVOL_DESDE = date(2026, 6, 1)
EVOL_MAX_MESES = 12

_SQL_EVOL = """
WITH me AS (
  SELECT LEAST((date_trunc('month', g) + interval '1 month - 1 day')::date,
               %(hoy)s::date) AS d
    FROM generate_series(%(desde)s::date, %(hoy)s::date, interval '1 month') g),
x AS (SELECT id_fact, fechaing, importe FROM scintela.chequesxfact
       WHERE fechaing > %(desde)s::date),
xd AS (SELECT x.id_fact, me.d, SUM(x.importe) AS despues
         FROM x JOIN me ON x.fechaing > me.d GROUP BY 1, 2),
f AS (SELECT f.id_factura, f.codigo_cli, f.fecha,
             -- Tamara 01/10: TNZ decía 134 días en el semáforo y 192 acá. Eran
             -- facturas T con centavos colgados: el saldo de HOY cuenta sólo
             -- en las vivas (las mismas que mira el semáforo); lo que se le
             -- aplicó después del cierre se suma igual.
             CASE WHEN f.stat IS NULL OR f.stat IN ('Z','A','',' ')
                  THEN COALESCE(f.saldo,0) ELSE 0 END AS saldo
        FROM scintela.factura f
       WHERE COALESCE(f.stat,'') <> 'X'
         AND COALESCE(f.usuario_crea,'') <> 'asinfo-backfill'
         AND (COALESCE(f.saldo,0) <> 0 OR f.id_factura IN (SELECT id_fact FROM x))),
s AS (SELECT me.d, f.codigo_cli, f.fecha, f.saldo + COALESCE(xd.despues,0) AS sal
        FROM me JOIN f ON f.fecha <= me.d
        LEFT JOIN xd ON xd.id_fact = f.id_factura AND xd.d = me.d)
SELECT codigo_cli AS cod, date_trunc('month', d)::date AS mes, SUM(sal) AS saldo,
       d - MIN(fecha) FILTER (WHERE sal >= %(centavos)s) AS edad
  FROM s GROUP BY codigo_cli, d
"""

_SQL_COMPRAS = """
SELECT f.codigo_cli AS cod, date_trunc('month', f.fecha)::date AS mes,
       SUM(f.importe) AS compro
  FROM scintela.factura f
 WHERE f.fecha >= %(desde)s::date AND f.fecha <= %(hoy)s::date
   AND f.importe > 0 AND COALESCE(f.stat,'') <> 'X'
   AND COALESCE(f.tipo,'') <> 'ND'
   AND COALESCE(f.usuario_crea,'') <> 'asinfo-backfill'
 GROUP BY 1, 2
"""


def _a_fecha(m):
    return m.date() if hasattr(m, "date") and not isinstance(m, date) else m


def evolucion(cods: set[str], evol: list[dict], compras: list[dict],
              por_mes: list[dict], meses: list[date],
              color_hoy: dict[str, str]) -> dict:
    """Serie mensual por cliente: saldo en facturas y factura más vieja al
    cierre, color de ese día, lo que compró y lo que pagó cada mes, y en
    cuántos días pagó.

    El color de los meses cerrados sale de las reglas que se pueden
    reconstruir (factura vieja y "debe y no compra"); el del mes en curso es
    el del semáforo de hoy, con todas las reglas.
    """
    idx = {m: i for i, m in enumerate(meses)}
    n = len(meses)
    out: dict[str, dict] = {}

    def fila(cod):
        return out.setdefault(cod, {"s": [0] * n, "e": [None] * n, "c": ["verde"] * n,
                                    "co": [0] * n, "pa": [0] * n, "di": [None] * n})
    for r in evol:
        i = idx.get(_a_fecha(r["mes"]))
        if i is None or r["cod"] not in cods:
            continue
        f = fila(r["cod"])
        f["s"][i] = round(_f(r["saldo"]))
        # Como el semáforo: si las notas de crédito dejan el saldo en
        # facturas en cero o a favor, la factura más vieja no cuenta (DCA, WUA).
        f["e"][i] = (None if r.get("edad") is None or _f(r["saldo"]) <= 0
                     else int(r["edad"]))
    compro_extra: dict[str, dict[date, float]] = {}
    for r in compras:
        if r["cod"] not in cods:
            continue
        m = _a_fecha(r["mes"])
        compro_extra.setdefault(r["cod"], {})[m] = _f(r["compro"])
        i = idx.get(m)
        if i is not None:
            fila(r["cod"])["co"][i] = round(_f(r["compro"]))
    for r in por_mes:
        i = idx.get(_a_fecha(r["mes"]))
        if i is None or r["cod"] not in cods:
            continue
        f = fila(r["cod"])
        f["pa"][i] += round(_f(r["plata"]))
        if _f(r["plata"]) >= 500:
            f["di"][i] = round(_f(r["dias"]))
    for cod, f in out.items():
        compras_cli = compro_extra.get(cod, {})
        for i, m in enumerate(meses):
            if i == n - 1 and cod in color_hoy:
                f["c"][i] = color_hoy[cod]
                continue
            e = f["e"][i]
            # compras de ese mes y los dos anteriores (≈ 90 días)
            atras = [date(m.year - (1 if m.month - k <= 0 else 0),
                          (m.month - k - 1) % 12 + 1, 1) for k in range(3)]
            venta90 = sum(compras_cli.get(a, 0) for a in atras)
            if (e is not None and e >= ROJO_DIAS_FACTURA) or (venta90 <= 0 and f["s"][i] > 500):
                f["c"][i] = "rojo"
            elif e is not None and e >= AMARILLO_DIAS_FACTURA:
                f["c"][i] = "amar"
    return out


def _meses_atras(hoy: date, n: int) -> list[date]:
    """Primeros de mes de los últimos `n` meses, del más viejo al actual."""
    out = []
    y, m = hoy.year, hoy.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def datos(hoy: date | None = None) -> dict:
    hoy = hoy or today_ec()
    meses = _meses_atras(hoy, 3)
    clientes = db.fetch_all(_SQL_CLIENTES, {"hoy": hoy, "centavos": CENTAVOS}) or []
    por_mes = db.fetch_all(_SQL_MESES, {"hoy": hoy, "desde": meses[0]}) or []
    medios = db.fetch_all(_SQL_MEDIOS, {"hoy": hoy}) or []
    d = armar(clientes, por_mes, medios, meses, hoy)
    # Evolución mes a mes (desde junio, hasta 12 meses).
    n = (hoy.year - EVOL_DESDE.year) * 12 + hoy.month - EVOL_DESDE.month + 1
    meses_e = _meses_atras(hoy, max(1, min(n, EVOL_MAX_MESES)))
    desde_c = _meses_atras(meses_e[0], 3)[0]
    evol = db.fetch_all(_SQL_EVOL, {"hoy": hoy, "desde": meses_e[0],
                                     "centavos": CENTAVOS}) or []
    compras = db.fetch_all(_SQL_COMPRAS, {"hoy": hoy, "desde": desde_c}) or []
    pm = db.fetch_all(_SQL_MESES, {"hoy": hoy, "desde": meses_e[0]}) or []
    cods = {p["k"] for p in d["puntos"]}
    d["evol"] = {"meses": [_MESES[m.month - 1][:3] for m in meses_e],
                 "cli": evolucion(cods, evol, compras, pm, meses_e,
                                  {c["cod"]: c["color"] for c in d["filas"]})}
    return d


# ── Cálculo (puro, testeable) ───────────────────────────────────────────────

def _f(x) -> float:
    return float(x or 0)


def _percentil(valores: list[float], p: float):
    v = sorted(valores)
    if not v:
        return None
    return v[int(p * (len(v) - 1))]


def _rango(v: float | None, universo: list[float]) -> float:
    """% del universo que está MEJOR (por debajo) que `v`."""
    if v is None or not universo:
        return 0.0
    return 100.0 * sum(1 for x in universo if x < v) / len(universo)


def semaforo(c: dict) -> tuple[str, list[str]]:
    """Color y motivos. Basta una condición; rojo le gana a amarillo.

    Tamara 30/09: *"que pasa si rompe mas de una regla?"* — el color es el
    peor, y los motivos son TODAS las reglas que rompe (primero las de
    rojo). La de la factura vieja va una sola vez, y el cheque devuelto
    también: si ya es rojo por viejo, no se repite como reciente.
    """
    rojo, amar = [], []
    edad = c.get("edad_max")
    if c["fac"] > 0 and edad is not None and edad >= ROJO_DIAS_FACTURA:
        rojo.append(f"Factura impaga de {edad} días")
    elif c["fac"] > 0 and edad is not None and edad >= AMARILLO_DIAS_FACTURA:
        amar.append(f"Factura impaga de {edad} días")
    if c["reb"] > 0 and (c.get("reb_dias") or 0) > REBOTE_VIEJO_DIAS:
        rojo.append("Cheque devuelto sin reemplazar hace más de "
                    f"{REBOTE_VIEJO_DIAS} días")
    elif c["reb"] > 0:
        amar.append("Cheque devuelto reciente sin reemplazar")
    if c["venta90"] <= 0 and c["fac"] > 500:
        rojo.append("Tiene facturas impagas y no compró en 90 días")
    if c.get("veces") is not None and c["veces"] >= AMARILLO_VECES_PLAZO:
        amar.append(f"Debe {c['veces']:.2f} veces lo normal para él".replace(".", ","))
    if c.get("cambio") is not None and c["cambio"] >= AMARILLO_EMPEORO_DIAS:
        amar.append(f"Pagó a {c['ultimo']} días; antes a {c['antes']}")
    if rojo:
        return "rojo", rojo + amar
    if amar:
        return "amar", amar
    return "verde", []


def armar(clientes: list[dict], por_mes: list[dict], medios: list[dict],
          meses: list[date], hoy: date) -> dict:
    # Plazo por mes de cada cliente.
    idx = {m: i for i, m in enumerate(meses)}
    serie: dict[str, list] = {}
    casa = [[0.0, 0.0] for _ in meses]  # [plata, plata*días]
    for r in por_mes:
        mes = r["mes"]
        mes = mes.date() if hasattr(mes, "date") and not isinstance(mes, date) else mes
        i = idx.get(mes)
        if i is None:
            continue
        plata, dias = _f(r["plata"]), _f(r["dias"])
        casa[i][0] += plata
        casa[i][1] += plata * dias
        if plata >= 500:
            serie.setdefault(r["cod"], [None] * len(meses))[i] = (round(dias), plata)

    filas = []
    # Los totales de la casa van sobre TODOS los clientes, antes de sacar
    # los de saldo chico de la lista.
    tot = {"facturas": 0.0, "cheques": 0.0, "rebotados": 0.0, "venta90": 0.0}
    for r in clientes:
        tot["facturas"] += _f(r["fac"])
        tot["cheques"] += _f(r["ch"])
        tot["rebotados"] += _f(r["reb"])
        tot["venta90"] += _f(r["venta90"])
    for r in clientes:
        c = {
            "cod": r["cod"], "nombre": (r["nombre"] or "")[:40],
            "vend": r["vend"], "cupo": _f(r["cupo"]),
            "stop": (r["stop"] or "").strip().upper(),
            "fac": _f(r["fac"]), "ch": _f(r["ch"]), "reb": _f(r["reb"]),
            "n_reb": int(r["n_reb"] or 0), "n_reb_hist": int(r["n_reb_hist"] or 0),
            "reb_dias": r.get("reb_dias"), "edad_max": r.get("edad_max"),
            "venta90": _f(r["venta90"]), "pagado": _f(r.get("pagado")),
            "dias_plata": None if r.get("dias_plata") is None else int(r["dias_plata"]),
        }
        c["deuda"] = c["fac"] + c["ch"]
        c["saldo"] = c["deuda"] + c["reb"]
        if c["saldo"] < SALDO_MINIMO and c["reb"] <= 0:
            continue
        c["dias_cartera"] = (c["deuda"] / (c["venta90"] / 90)) if c["venta90"] > 0 else None
        c["veces"] = None
        if (c["dias_plata"] is not None and c["pagado"] >= PAGADO_MINIMO_HISTORIA
                and c["dias_cartera"] is not None):
            c["veces"] = c["dias_cartera"] / max(c["dias_plata"], 15)
        c["uso_cupo"] = c["saldo"] / c["cupo"] if c["cupo"] > 0 else None
        # Contra sí mismo: último mes con pagos vs. el promedio de los anteriores.
        s = serie.get(c["cod"])
        c["meses"] = s or [None] * len(meses)
        c["cambio"] = c["ultimo"] = c["antes"] = None
        if s:
            ult = max((i for i, x in enumerate(s) if x), default=None)
            if ult:
                prev = [x for x in s[:ult] if x]
                pw = sum(x[1] for x in prev)
                if pw >= 1000:
                    c["antes"] = round(sum(x[0] * x[1] for x in prev) / pw)
                    c["ultimo"] = s[ult][0]
                    c["cambio"] = c["ultimo"] - c["antes"]
        filas.append(c)

    # Puntaje: cada factor, contra el resto de los clientes.
    u_edad = [c["edad_max"] for c in filas if c["fac"] > 0 and c["edad_max"] is not None]
    u_veces = [c["veces"] for c in filas if c["veces"] is not None]
    u_cambio = [c["cambio"] for c in filas if c["cambio"] is not None]
    for c in filas:
        partes = [((_rango(c["edad_max"], u_edad)
                    if c["fac"] > 0 and c["edad_max"] is not None else 0.0),
                   PESO_ANTIGUEDAD)]
        if c["veces"] is not None:
            partes.append((_rango(c["veces"], u_veces), PESO_DEUDA_VS_PLAZO))
        if c["cambio"] is not None:
            partes.append((_rango(c["cambio"], u_cambio), PESO_TENDENCIA))
        p = sum(v * w for v, w in partes) / sum(w for _, w in partes)
        if c["reb"] > 0:
            p = max(p, 90 if (c["reb_dias"] or 0) > REBOTE_VIEJO_DIAS else 75)
        elif c["n_reb_hist"] > 0:
            p = min(100, p + 10)
        if c["venta90"] <= 0 and c["fac"] > 500:
            p = max(p, 90)
        c["puntaje"] = round(p)
        c["color"], c["motivos"] = semaforo(c)

    orden = {"rojo": 0, "amar": 1, "verde": 2}
    filas.sort(key=lambda c: (orden[c["color"]], -c["puntaje"], -c["saldo"]))

    def _resumen(lista):
        total = sum(max(c["saldo"], 0) for c in lista) or 1.0
        r = {k: {"n": 0, "saldo": 0.0} for k in orden}
        for c in lista:
            r[c["color"]]["n"] += 1
            r[c["color"]]["saldo"] += max(c["saldo"], 0)
        for k in r:
            r[k]["pct"] = round(100 * r[k]["saldo"] / total)
            r[k]["saldo"] = round(r[k]["saldo"])
        return r

    resumen = _resumen(filas)
    # Tamara 30/09: las tarjetas del semáforo también siguen al Vendedor.
    resumen_vend = {v: _resumen([c for c in filas if c["vend"] == v])
                    for v in {c["vend"] for c in filas}}

    cartera = {k: tot[k] for k in ("facturas", "cheques", "rebotados")}
    cartera["total"] = cartera["facturas"] + cartera["cheques"] + cartera["rebotados"]
    cartera["dias"] = (round(cartera["total"] / (tot["venta90"] / 90))
                       if tot["venta90"] else None)
    grandes = sorted(filas, key=lambda c: -c["saldo"])[:10]
    cartera["top10_pct"] = round(100 * sum(max(c["saldo"], 0) for c in grandes)
                                 / (cartera["total"] or 1))
    cartera["top10"] = [c["cod"] for c in grandes[:5]]

    def _bench(vals, fmt):
        return [fmt(_percentil(vals, p)) for p in (0.5, 0.75, 0.9)]

    normal = {
        "edad": _bench(u_edad, lambda v: None if v is None else round(v)),
        "veces": _bench(u_veces, lambda v: None if v is None else round(v, 2)),
        "cambio": _bench(u_cambio, lambda v: None if v is None else round(v)),
        "plazo": _bench([c["dias_plata"] for c in filas
                         if c["dias_plata"] is not None
                         and c["pagado"] >= PAGADO_MINIMO_HISTORIA],
                        lambda v: v),
    }

    prioridad = sorted(
        [c for c in filas if c["puntaje"] >= COBRAR_PRIMERO_PUNTAJE
         and c["saldo"] >= COBRAR_PRIMERO_SALDO],
        key=lambda c: -(c["puntaje"] * (c["saldo"] ** 0.25)))

    return {
        "hoy": hoy,
        "filas": filas,
        # Lo que dibuja el gráfico de "Cobrar primero" (sólo con saldo).
        "puntos": [{"k": c["cod"], "n": c["nombre"], "v": c["vend"],
                    "c": c["color"], "s": round(c["saldo"]),
                    "p": c["puntaje"],
                    "m": c["motivos"]}
                   for c in filas if c["saldo"] > 0],
        "resumen": resumen,
        "resumen_vend": resumen_vend,
        "cartera": cartera,
        "normal": normal,
        "prioridad": prioridad,
        "meses": [_MESES[m.month - 1] for m in meses],
        "casa_meses": [
            {"mes": _MESES[m.month - 1], "plata": p,
             "dias": round(pd / p) if p else None}
            for m, (p, pd) in zip(meses, casa, strict=True)],
        "medios": [{"medio": r["medio"], "plata": _f(r["plata"]),
                    "entrega": round(_f(r["entrega"])),
                    "disponible": round(_f(r["disponible"]))} for r in medios],
        "contra_si": sorted([c for c in filas if c["cambio"] is not None
                             and c["saldo"] >= 5000],
                            key=lambda c: -c["cambio"]),
        "cupos": sorted([c for c in filas if c["uso_cupo"] and c["uso_cupo"] > 1],
                        key=lambda c: -(c["saldo"] - c["cupo"])),
        "rebotes": sorted([c for c in filas if c["reb"] > 0 or c["n_reb_hist"] > 0],
                          key=lambda c: (-c["reb"], -c["n_reb_hist"])),
        "limites": {
            "rojo_dias": ROJO_DIAS_FACTURA, "amar_dias": AMARILLO_DIAS_FACTURA,
            "veces": AMARILLO_VECES_PLAZO, "empeoro": AMARILLO_EMPEORO_DIAS,
            "rebote": REBOTE_VIEJO_DIAS,
        },
    }
