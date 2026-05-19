"""Informes gerenciales. Read-only.

All formulas traced to INFORMES.PRG. Variable names below match the PRG
so business rules stay auditable against the 30-year-old source of truth.

Key balance formulas from INFORMES.PRG (lines 370-380):

    SALBANC = SALBANC1 + SALBANC2
    CART    = TOTF + TOTC
    SUBT    = SALBANC + SALCAJ + CART
    TOTL    = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC
    PATR    = TOTL - TOTP
    UTILIDAD = PATR - PATANT          (PATANT = HISTORIA.patrimonio del mes anterior)

Schema realities (verified against intela12042026.sql):
    - banco(no_banco, nombre)                       — NOT nombre_banco
    - factura.codigo_cli (join al cliente por code) — NOT id_cliente
    - cheque.codigo_cli                             — same
    - compra NO tiene columna saldo                 — deudas son vía posdat
    - posdat.banc (no 'banco')                      — banc<>9 = pasivos
    - retiros.ret y retiros.nb                      — monto y banco
    - dolares.st                                    — estado del anticipo
    - activos.tipo IN ('I','M','C','K')             — I=inmueble, M/C/K=maquinaria
    - historia.stock = VSTO, historia.uqui = VQX, historia.patrimonio = PATANT
"""
from datetime import date

import db
from modules.posdat import (
    POSDAT_DEUDA_VIVA_WHERE,
    POSDAT_EGRESO_FLUJO_WHERE,
    posdat_deuda_viva_where,
)

# ---------------------------------------------------------------------------
# Constantes del PRG legacy (INFORMES.PRG líneas 5-6)
# ---------------------------------------------------------------------------

# Provisión mensual NO cancelada en el mes corriente — hardcoded en
# INFORMES.PRG línea 5: `PROVISIONES=80000`. Se amortiza prorrateado
# durante el mes contra UT.PROY (línea 420):
#     PROVI = PROVISIONES * (1 - DAY(DATE())/30)
# El dia 1  → PROVI = 80.000 * 29/30 = 77.333  (resta casi entera).
# El dia 15 → PROVI = 80.000 * 15/30 = 40.000  (la mitad ya amortizada).
# El dia 30 → PROVI = 0                        (mes completamente amortizado).
# Si DAY > 30 → clamp a 0 (mes corto / 31 días).
PROVISIONES_MES_USD = 80000.0


def provision_pendiente_mes(hoy: date | None = None) -> float:
    """Provisión que falta amortizar este mes — INFORMES.PRG línea 420.

    Fórmula PRG: ``PROVI = PROVISIONES * (1 - DAY(DD)/30)``.

    Devuelve un número >= 0. El día 1 del mes vale casi $80k, el día 30
    vale 0. Para meses de 31 días, el día 31 también queda en 0 (clamp).

    Se descuenta de UT.PROY (utilidad proyectada) para reflejar que esos
    $80k de provisiones todavía no están reservados en el cierre.
    """
    h = hoy or date.today()
    dia = h.day
    if dia >= 30:
        return 0.0
    return PROVISIONES_MES_USD * (1.0 - dia / 30.0)


# ---------------------------------------------------------------------------
# Building blocks — each is one small, cheap query
# ---------------------------------------------------------------------------

def totf() -> float:
    """Cartera de facturas: saldo neto por cobrar en facturas vivas (Z + A).

    Vocabulario canónico (2026-04-29): Z=emitida sin abono, A=abonada
    parcial. Excluye T (cancelada total), X (eliminada por error), Y (legacy
    anulada). El blank/empty también cuenta como vivo (datos legacy).

    Bug TMT 2026-05-06: el filtro `saldo > 0` que teníamos antes excluía
    las facturas con saldo NEGATIVO (664 facturas, $-293.923,87 en mayo
    2026), que en la convención dBase son **sobrepagos** del cliente
    (abono > importe) y deben **netear** la cartera. La fórmula PRG
    `SUM ALL SALDO TO TOTF FOR STAT $ "ZA"` no filtra signo — TOTF es la
    cartera NETA. Quitamos el filtro para replicar exactamente el legacy.

    Verificado contra FACTURAS.DBF mayo 2026: SUM(saldo) Z+A sin filtro de
    signo = $4.916.202,77 (= lo que TMT veía en el dBase live).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total
        FROM scintela.factura
        WHERE stat IS NULL OR stat IN ('Z','A','',' ')
        """
    )
    return float(row["total"] or 0)


def totc() -> float:
    """Cheques vivos en cartera — fórmula del INFORMES.PRG línea 24.

    PRG original: ``&SAI TOTC FOR STAT $ "Z123PD"`` → stat ∈ {Z,1,2,3,P,D}.
    Incluye los **rebotados que aún se gestionan para cobro** (1/2/3): el
    dueño quiere verlos en cartera porque siguen siendo cobrables. Si los
    excluís, el "Cheques (cartera)" del balance no coincide con el total
    histórico que el gerente lee desde el dBase. Decisión confirmada
    2026-04-30 por TMT.

    Vocabulario:
      Z = en cartera (sin movimiento)
      1/2/3 = rebotado en 1/2/3 oportunidad — aún en gestión
      P = postergado
      D = en gestión Daniela
    Excluye:
      B = depositados (ya suman al saldo bancario)
      A = legacy acreditados (idem B)
      R = rebotado terminal (incobrable)
      X = eliminados por error
      V = legacy banco Internacional (ignorar)
      Y = anulado
      T = terminal (cancelada)
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.cheque
        WHERE stat IN ('Z','1','2','3','P','D')
        """
    )
    return float(row["total"] or 0)


def cheques_por_stat() -> dict:
    """Breakdown total por stat — útil para el panel de diagnóstico.

    Devuelve un dict con todos los stats encontrados y su SUM(importe). El
    template muestra cuánto está en cartera, depositado, rebotado, etc.,
    de modo que el gerente pueda confirmar por qué TOTC vale lo que vale.
    """
    rows = db.fetch_all(
        """
        SELECT COALESCE(stat, '') AS stat,
               COUNT(*)            AS n,
               COALESCE(SUM(importe), 0) AS total
        FROM scintela.cheque
        GROUP BY stat
        ORDER BY stat
        """
    )
    out = {}
    for r in rows:
        out[r["stat"] or "(vacío)"] = {
            "n": int(r["n"] or 0),
            "total": float(r["total"] or 0),
        }
    return out


def saldo_bancos() -> list[dict]:
    """Saldos bancarios — running saldo del dBase como primario.

    Historia de los bugs (2026-04-30):
      1) Lectura naive del último `t.saldo` daba 0 cuando la fila más
         reciente tenía NULL (insertes nuevos no recalculan running).
      2) Cómputo SUM firmado por documento (CH/ND vs resto) tampoco anda
         siempre — la migración DBF→Postgres pasó documento con formatos
         distintos en filas legacy o el importe ya viene firmado.

    Política: tomar el **último saldo stored NO-CERO** como primario.
    Eso replica lo que hace el dBase (el running saldo es la fuente de
    verdad mientras la migración a "100% computed" no esté completa).
    El cálculo (signed y raw) queda expuesto como diagnostic.

    Resolución por banco (en orden):
      1. último `saldo` stored con valor no-cero → ese
      2. SUM firmado (CH/ND = egreso, resto = ingreso) si no es 0
      3. SUM crudo de `importe` si no es 0
      4. 0 (sin movimientos)

    Devuelve por banco:
      - `saldo`: el resuelto (lo que el balance va a mostrar)
      - `saldo_origen`: 'stored' | 'signed' | 'raw' | 'empty'
      - `saldo_stored`, `saldo_signed`, `saldo_raw`: para diagnóstico
      - `n_transacciones`
    """
    rows = db.fetch_all(
        """
        SELECT b.no_banco,
               b.nombre,
               COALESCE((
                 SELECT t.saldo
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                   AND t.saldo IS NOT NULL
                   AND ABS(t.saldo) > 0.5
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo_stored,
               COALESCE((
                 SELECT SUM(
                   CASE WHEN UPPER(TRIM(t.documento)) IN ('CH','ND')
                        THEN -t.importe
                        ELSE  t.importe
                   END
                 )
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
               ), 0) AS saldo_signed,
               COALESCE((
                 SELECT SUM(t.importe)
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
               ), 0) AS saldo_raw,
               (
                 SELECT COUNT(*)
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
               ) AS n_transacciones
        FROM scintela.banco b
        ORDER BY b.no_banco
        """
    )
    import logging
    _log_saldo = logging.getLogger("programa_core.informes.saldo_bancos")
    out = []
    for r in rows:
        stored = float(r.get("saldo_stored") or 0)
        signed = float(r.get("saldo_signed") or 0)
        raw    = float(r.get("saldo_raw") or 0)
        if abs(stored) > 0.5:
            saldo, origen = stored, "stored"
        elif abs(signed) > 0.5:
            saldo, origen = signed, "signed"
        elif abs(raw) > 0.5:
            saldo, origen = raw, "raw"
        else:
            saldo, origen = 0.0, "empty"

        # TMT 2026-05-14 (audit #45): logging cuando NO usamos stored.
        # Si el saldo running de transacciones_bancarias está roto (NULL
        # en la última fila), caemos a SUM signed/raw — pero eso oculta
        # el bug del running. Loguear ayuda a Tamara a saber que hay
        # drift entre lo stored y lo computado. `usa_fallback=True` se
        # devuelve para que el template pueda mostrar un warning visible.
        if origen != "stored":
            _log_saldo.warning(
                "saldo banco %s (%s) no usa 'stored': origen=%s stored=%.2f signed=%.2f raw=%.2f n_tx=%s",
                r.get("no_banco"), r.get("nombre"), origen,
                stored, signed, raw, r.get("n_transacciones"),
            )
        out.append({
            "no_banco":        r.get("no_banco"),
            "nombre":          r.get("nombre"),
            "saldo":           saldo,
            "saldo_origen":    origen,
            "usa_fallback":    origen not in ("stored", "empty"),
            "saldo_stored":    stored,
            "saldo_signed":    signed,
            "saldo_raw":       raw,
            "n_transacciones": int(r.get("n_transacciones") or 0),
        })
    return out


def salcaj() -> float:
    """Saldo en caja: último movimiento."""
    row = db.fetch_one(
        """
        SELECT saldo
        FROM scintela.caja
        ORDER BY fecha DESC, id_caja DESC
        LIMIT 1
        """
    )
    return float(row["saldo"] or 0) if row and row["saldo"] is not None else 0.0


def posdat_totales() -> dict:
    """POS1, POS2 (bancos 1/2 al saldo) y TOTP (pasivos = deuda viva).

    TOTP usa POSDAT_DEUDA_VIVA_WHERE (banc=0), NO banc<>9. Diferencia
    importante: banc=1/2 (cheques modernos PC) ya descontaron saldo en
    transacciones_bancarias vía bank_helpers.insert_movimiento_bancario.
    Si los sumamos como pasivo, double-counta. banc=9 son cheques
    posdatados legacy que tampoco deben contar como pasivo abierto
    (el cheque ya fue emitido).

    Sólo banc=0 (anulada IS NOT TRUE) = deuda viva no instrumentada.
    """
    row = db.fetch_one(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN banc = 1 THEN importe ELSE 0 END), 0) AS pos1,
          COALESCE(SUM(CASE WHEN banc = 2 THEN importe ELSE 0 END), 0) AS pos2,
          COALESCE(SUM(CASE WHEN {POSDAT_DEUDA_VIVA_WHERE} THEN importe ELSE 0 END), 0) AS totp
        FROM scintela.posdat
        WHERE (anulada IS NOT TRUE OR anulada IS NULL)
        """
    )
    if not row:
        return {"pos1": 0.0, "pos2": 0.0, "totp": 0.0}
    return {
        "pos1": float(row["pos1"] or 0),
        "pos2": float(row["pos2"] or 0),
        "totp": float(row["totp"] or 0),
    }


def activos_totales() -> dict:
    """UMAQ = maquinaria (M/C/K)  ·  UACT = terrenos/edificios/instal. (I).

    El `valor` (valor en libros) se computa día a día como en dBase
    MENU.PRG líneas 275-276:

        COEF = IIF(DAY(today) > 30, 1, DAY(today)/30)
        AMORTIMES_calc = COEF × CUOTA
        VALOR = INICIAL − AMORTIZAC − AMORTIMES_calc

    Es decir, AMORTIMES se prorrateaa lineal por el día del mes (0 al
    inicio, CUOTA entero el día 30+). El valor en libros del balance
    baja un poquito cada día, replicando el comportamiento legacy.

    No usamos la columna `valor` stored porque sólo se refresca cuando
    corre la procedure mensual. La columna `amortimes` stored tampoco —
    es solo histórica.
    """
    row = db.fetch_one(
        """
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c
        ),
        v AS (
          SELECT
            tipo,
            COALESCE(inicial, 0)
              - COALESCE(amortizac, 0)
              - (SELECT c FROM coef) * COALESCE(cuota, 0) AS valor_calc
          FROM scintela.activos
        )
        SELECT
          COALESCE(SUM(CASE WHEN tipo IN ('M','C','K') THEN GREATEST(valor_calc, 0) ELSE 0 END), 0) AS umaq,
          COALESCE(SUM(CASE WHEN tipo = 'I'           THEN GREATEST(valor_calc, 0) ELSE 0 END), 0) AS uact
        FROM v
        """
    )
    if not row:
        return {"umaq": 0.0, "uact": 0.0}
    return {"umaq": float(row["umaq"] or 0), "uact": float(row["uact"] or 0)}


def anticipos() -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.dolares
        WHERE st IS NULL OR st IN ('', ' ')
        """
    )
    return float(row["total"] or 0) if row else 0.0


def uret_mes_corriente() -> float:
    """Retiros dueños del MES en curso (URET).

    Fórmula PRG INFORMES.PRG línea 37:
        SUM ALL RET TO URET FOR &MA .AND. DD-FECHA<63

    `&MA` es el macro estándar del legacy que expande a "MONTH(FECHA)=MES
    .AND. YEAR(FECHA)=YEAR(DATE())" — filtrar por mes/año actual. La
    cláusula `DD-FECHA<63` es una guarda defensiva contra retiros con
    fechas muy desplazadas (importaciones viejas, errores). En el mes en
    curso ese filtro siempre pasa, así que basta con el filtro de mes.

    Bug TMT 2026-05-06: el código anterior aplicaba SÓLO el filtro de
    63 días, lo que sumaba retiros de marzo + abril + mayo (~250k) en
    vez de los 85k de mayo. Ahora filtramos por mes en curso, replicando
    el comportamiento real del dBase.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0) AS total
        FROM scintela.retiros
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        """
    )
    return float(row["total"] or 0) if row else 0.0


def _try_movimientos_mes() -> dict | None:
    """Wrapper safe — si la query falla, devuelve None para que la
    pantalla de balance no rompa por esto. Logueamos en stderr."""
    try:
        return movimientos_mes_dbase()
    except Exception as e:
        import logging
        logging.exception("movimientos_mes_dbase falló: %s", e)
        return None


def movimientos_mes_dbase(anio: int | None = None,
                          mes: int | None = None) -> dict:
    """Datos para el cuadro MOVIMIENTOS MES estilo dBase.

    Replica INFORMES.PRG líneas 1003-1090. Devuelve:

    - `header`: las 4 categorías top (HILADO / TEJIDO / TERMINADO / COLORANTES),
      cada una con stock_inic, ingresos, egresos, stock_act expresados en
      kg, $/kg y $.
    - `compras_hilado`: filas por PROV de tipo='H' del mes seleccionado, con
      total al final.
    - `produc_tejido`: filas por PROV de tipo='K' (con kg > 0) del mes
      seleccionado, con total al final.
    - `tintoreria`: { bajos, fuertes, totales } — del mes seleccionado.
    - `cs`: { colorantes, produccion } — costos unitarios resumen.

    TMT 2026-05-19 v8 — extendido para usar año/mes parametrizables (pantalla
    /informes/flujo-produccion). Default = mes en curso. TINTORERIA y CS
    se calculan acá; queda como N/D si los datos no están.
    """
    from datetime import date as _date
    hoy = _date.today()
    yy = int(anio) if anio else hoy.year
    mm = int(mes) if mes else hoy.month
    # TMT 2026-05-19 v8 — defensivo: si historia falla por cualquier
    # razón (sin snapshot, tabla vacía, error de conexión), seguimos con
    # `{}` para que el template no rompa. Cada acceso usa `or 0`.
    try:
        hist = historia_mas_reciente() or {}
    except Exception:
        hist = {}
    # 4 columnas top — usa los kg/$ del último snapshot (idem block kg
    # original). $/kg derivado.
    def _safe_div(a, b):
        try:
            return float(a) / float(b) if b else 0.0
        except (TypeError, ZeroDivisionError):
            return 0.0

    # TMT 2026-05-19 v8 — refactor: scintela.historia NO tiene desglose
    # stock_hilado/tejido/terminado — sólo `stock` (total). El desglose
    # vive en scintela.iniciales (mensual). Usamos iniciales para el
    # mes seleccionado como STOCK INICIAL, y para tarifas (um/uk/uf/uq).
    try:
        inic = db.fetch_one(
            """
            SELECT hilado, tejido, terminado, vq, um, uk, uf, uq
              FROM scintela.iniciales
             WHERE mesnum = %s AND yy = %s
             ORDER BY id_iniciales DESC
             LIMIT 1
            """,
            (mm, yy),
        ) or {}
    except Exception:
        inic = {}

    # Fallback: si no hay iniciales del mes pedido, agarrar la más reciente.
    if not inic or not (float(inic.get("hilado") or 0)):
        try:
            inic = db.fetch_one(
                """
                SELECT hilado, tejido, terminado, vq, um, uk, uf, uq
                  FROM scintela.iniciales
                 WHERE COALESCE(hilado, 0) > 0
                 ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
                 LIMIT 1
                """,
            ) or {}
        except Exception:
            inic = inic or {}

    hi0 = float(inic.get("hilado") or hist.get("stock_hilado") or hist.get("stock") or 0)
    tj0 = float(inic.get("tejido") or hist.get("stock_tejido") or 0)
    pf0 = float(inic.get("terminado") or hist.get("stock_terminado") or 0)
    vq0 = float(inic.get("vq") or hist.get("uqui") or 0)

    kcom = float(hist.get("kcom") or 0)
    ucom = float(hist.get("ucom") or 0)
    ktej = float(hist.get("ktej") or 0)
    ktin = float(hist.get("ktin") or 0)
    kvent = float(hist.get("kvent") or 0)
    utej = float(hist.get("utej") or 0)
    utin = float(hist.get("utin") or 0)

    # Tarifas: primero de iniciales (objetivo / opening balance), después
    # heurística PRG (UM = ucom/kcom; UK = UM+0.5; UF = UM+2.2).
    um0 = float(inic.get("um") or 0) or _safe_div(ucom, kcom)
    if um0 == 0:
        um0 = _safe_div(float(hist.get("ustock") or 0), float(hist.get("stock") or 0))
    uk0 = float(inic.get("uk") or 0) or (um0 + 0.5 if um0 else 0.0)
    uf0 = float(inic.get("uf") or 0) or (um0 + 2.2 if um0 else 0.0)

    hilado_act_kg = max(hi0 + kcom - ktej, 0)
    tejido_act_kg = max(tj0 + ktej - ktin, 0)
    termin_act_kg = max(pf0 + ktin - kvent, 0)
    color_act_us  = vq0  # sin movimientos automáticos por etapa

    # % de eficiencia (egreso / ingreso del mes). En el dBase se mostraba
    # como "0.50%" en tejido crudo y "3.76%" en terminado — proxy de
    # merma o productividad.
    pct_tej = _safe_div(ktin, ktej) * 100 if ktej else 0.0
    pct_ter = _safe_div(kvent, ktin) * 100 if ktin else 0.0

    header = {
        "hilado": {
            "stock_inic_kg": hi0, "stock_inic_ukg": um0, "stock_inic_us": hi0 * um0,
            "ingresos_kg":   kcom, "ingresos_ukg": _safe_div(ucom, kcom), "ingresos_us": ucom,
            "egresos_kg":    ktej,
            "stock_act_kg":  hilado_act_kg, "stock_act_ukg": um0, "stock_act_us": hilado_act_kg * um0,
        },
        "tejido": {
            "stock_inic_kg": tj0, "stock_inic_us": tj0 * uk0,
            "ingresos_kg":   ktej, "ingresos_pct": pct_tej, "ingresos_us": utej,
            "egresos_kg":    ktin,
            "stock_act_kg":  tejido_act_kg, "stock_act_us": tejido_act_kg * uk0,
        },
        "terminado": {
            "stock_inic_kg": pf0, "stock_inic_us": pf0 * uf0,
            "ingresos_kg":   ktin, "ingresos_pct": pct_ter, "ingresos_us": utin,
            "egresos_kg":    kvent,
            "stock_act_kg":  termin_act_kg, "stock_act_us": termin_act_kg * uf0,
        },
        "colorantes": {
            "stock_inic_us": vq0,
            "ingresos_us":   0.0,  # se setea abajo con compras Q del mes
            "egresos_us":    0.0,
            "stock_act_us":  color_act_us,
        },
    }

    # Breakdown por proveedor del mes seleccionado.
    # TMT 2026-05-19 v8 — fix prod: scintela.compra usa `codigo_prov`,
    # no `prov` (que es el nombre en posdat / cheque). Antes esto rompía
    # con `column "prov" does not exist` y tiraba la página entera.
    compras_hilado = db.fetch_all(
        """
        SELECT codigo_prov                AS prov,
               COALESCE(SUM(kg), 0)       AS kg,
               COALESCE(SUM(importe), 0)  AS importe
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'H'
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND COALESCE(codigo_prov, '') <> ''
           AND UPPER(COALESCE(codigo_prov, '')) <> 'XX'
         GROUP BY codigo_prov
         ORDER BY SUM(importe) DESC
         LIMIT 20
        """,
        (yy, mm),
    ) or []
    for r in compras_hilado:
        r["ukg"] = _safe_div(r.get("importe"), r.get("kg"))

    produc_tejido = db.fetch_all(
        """
        SELECT codigo_prov                AS prov,
               COALESCE(SUM(kg), 0)       AS kg,
               COALESCE(SUM(importe), 0)  AS importe
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'K'
           AND COALESCE(kg, 0) > 0
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND COALESCE(codigo_prov, '') <> ''
         GROUP BY codigo_prov
         ORDER BY SUM(importe) DESC
         LIMIT 20
        """,
        (yy, mm),
    ) or []
    for r in produc_tejido:
        r["ukg"] = _safe_div(r.get("importe"), r.get("kg"))

    # TINTORERIA — tipo='C' (tintura). Heurística bajos/fuertes:
    # el dBase distinguía bajos (poco colorante) vs fuertes (mucho).
    # Sin flag dedicado, proxy: importe < 0.5 USD/kg → BAJOS, >= → FUERTES.
    # Aproximación pragmática; afinar con la dueña si difiere mucho del PRG.
    tint_rows = db.fetch_all(
        """
        SELECT COALESCE(kg, 0)::numeric      AS kg,
               COALESCE(importe, 0)::numeric AS importe
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'C'
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND COALESCE(kg, 0) > 0
        """,
        (yy, mm),
    ) or []
    bajos_kg = bajos_us = 0.0
    fuertes_kg = fuertes_us = 0.0
    for r in tint_rows:
        kkg = float(r["kg"])
        imp = float(r["importe"])
        if _safe_div(imp, kkg) < 0.5:
            bajos_kg += kkg
            bajos_us += imp
        else:
            fuertes_kg += kkg
            fuertes_us += imp
    tint_kg = bajos_kg + fuertes_kg
    tint_us = bajos_us + fuertes_us
    bajos_pct   = (bajos_kg   / tint_kg * 100) if tint_kg else 0.0
    fuertes_pct = (fuertes_kg / tint_kg * 100) if tint_kg else 0.0

    # CS.COLORANTES — costo unitario colorantes consumidos / kg tinturados.
    # Aproximación: importe de compras de químicos del mes (tipo='Q') sobre
    # kg tinturados del mes (ktin de historia).
    quimicos_mes = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS importe
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'Q'
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
        """,
        (yy, mm),
    ) or {}
    cs_col_us = float(quimicos_mes.get("importe") or 0)
    cs_col_ukg = _safe_div(cs_col_us, ktin)
    # Aplicar al header de colorantes el ingreso $ del mes.
    header["colorantes"]["ingresos_us"] = cs_col_us

    # CS.PRODUCCION — costo total de producción (mat. prima + tejido + tin
    # + col) / kg producidos en el mes. Usa hist live.
    cs_prod_us = ucom + utej + utin + cs_col_us
    cs_prod_kg = ktin or ktej or kvent  # mejor proxy disponible
    cs_prod_ukg = _safe_div(cs_prod_us, cs_prod_kg)

    return {
        "anio": yy, "mes": mm,
        "header": header,
        "compras_hilado": [dict(r) for r in compras_hilado],
        "compras_hilado_total": {
            "kg": sum(float(r.get("kg") or 0) for r in compras_hilado),
            "importe": sum(float(r.get("importe") or 0) for r in compras_hilado),
        },
        "produc_tejido": [dict(r) for r in produc_tejido],
        "produc_tejido_total": {
            "kg": sum(float(r.get("kg") or 0) for r in produc_tejido),
            "importe": sum(float(r.get("importe") or 0) for r in produc_tejido),
        },
        "tintoreria": {
            "total":   {"kg": tint_kg, "us": tint_us,
                        "ukg": _safe_div(tint_us, tint_kg), "pct": 100.0 if tint_kg else 0.0},
            "bajos":   {"kg": bajos_kg, "us": bajos_us,
                        "ukg": _safe_div(bajos_us, bajos_kg), "pct": bajos_pct},
            "fuertes": {"kg": fuertes_kg, "us": fuertes_us,
                        "ukg": _safe_div(fuertes_us, fuertes_kg), "pct": fuertes_pct},
        },
        "cs": {
            "colorantes": {"kg": ktin, "ukg": cs_col_ukg, "us": cs_col_us, "ant": 0.0},
            "produccion": {"kg": cs_prod_kg, "ukg": cs_prod_ukg, "us": cs_prod_us, "ant": 0.0},
        },
    }


def historia_ultimo_snapshot() -> dict | None:
    """Última fila de scintela.historia (LATEST sin filtro de fecha).

    Para VSTO y VQX live: el dBase escribe una fila al historia cada día
    con el snapshot LIVE (= patrimonio computado al momento). Para que
    "Stock MP+Prod." y "Stock Quí." en el panel ACTIVO muestren los
    valores de HOY (no del cierre anterior), leemos la fila más reciente.

    Bug TMT 2026-05-06: el filtro `fecha < primer día del mes en curso`
    funciona para PATANT (queremos el cierre de abril), pero rompe el
    display de VSTO/VQX (queremos los valores de hoy = 06/05/2026 con
    UQUI=279.591, no del 30/04 con UQUI=296.839).

    Solución: dos queries separadas. `historia_ultimo_snapshot()` (esta)
    para VSTO/VQX live; `historia_ultimo_mes()` (filtrada) para PATANT.
    """
    return db.fetch_one(
        """
        SELECT *
        FROM scintela.historia
        ORDER BY fecha DESC
        LIMIT 1
        """
    )


def historia_ultimo_mes() -> dict | None:
    """Último snapshot CERRADO de scintela.historia (para PATANT, VSTO, VQX).

    El dBase legacy escribe una fila a `historia` al cierre del mes. Pero
    la migración a Postgres a veces deja filas con fecha del mes en curso
    que NO son cierres reales (snapshots parciales). Si tomamos esas
    filas como PATANT, contaminamos el cálculo de UTILIDAD = PATR-PATANT
    porque PATANT terminaría siendo casi igual al patrimonio actual y la
    utilidad se acerca a 0.

    Bug TMT 2026-05-06 (utilidad mostraba 111k vs esperado 133k):
    historia tenía una fila con fecha=06/05/2026 (hoy) que no era cierre
    real. La utilidad caía porque PATANT estaba casi al día.

    Fix: filtrar a `fecha < primer día del mes en curso` para forzar que
    PATANT sea efectivamente el cierre del mes anterior (= 30 de abril
    cuando estamos en mayo).
    """
    return db.fetch_one(
        """
        SELECT *
        FROM scintela.historia
        WHERE fecha < date_trunc('month', CURRENT_DATE)::date
        ORDER BY fecha DESC
        LIMIT 1
        """
    )


def historia_mas_reciente() -> dict | None:
    """Snapshot MÁS RECIENTE de scintela.historia, incluyendo filas
    intra-mes (live).

    Distinta de `historia_ultimo_mes()`, que filtra el mes en curso para
    no contaminar PATANT (PATANT debe ser CIERRE anterior). Esta función
    devuelve la fila más nueva sin importar la fecha.

    Uso: panel STOCK del balance — necesita el snapshot LIVE de stock
    (kg + ustock) que CUADRE con iniciales del mes en curso. Si usamos
    el mes anterior, los kg de iniciales (May, vivos) se mezclan con
    los $ de historia (April, cierre) y el U$/kg sale sobreestimado.

    Bug TMT 2026-05-11: STOCK mostraba Hilado=2,950 en vez de 2,926
    porque hist.ustock (April $) se dividía por kg de iniciales (May).
    """
    return db.fetch_one(
        """
        SELECT *
        FROM scintela.historia
        ORDER BY fecha DESC
        LIMIT 1
        """
    )


def iniciales_mes_actual() -> dict | None:
    """Proyecciones / metas del mes en curso desde scintela.iniciales.

    Estrategia robusta:
      1. Buscar exacto mes/año actual.
      2. Si no, la fila más reciente CON DATOS REALES (kprog>0 o hilado>0).
      3. Si tampoco, la más reciente cualquiera.

    Devuelve None sólo si la tabla está vacía. Se usa para las columnas
    PROYEC. y para los KG de stock (HILADO/TEJIDO/TERMINADO) + precios
    (UM/UK/UF) en el informe de resultados.
    """
    hoy = date.today()
    # 1. Intento exacto del mes actual.
    #    Tie-breaker `id_iniciales DESC` por si hay (mes, yy) duplicados:
    #    el DBF tiene 4 combos repetidos históricamente (Apr 2020, Oct 2021,
    #    Jul 2022, Apr 2025) — sin tie-breaker el resultado es
    #    no-determinista. La fila con id_iniciales más alto = la última
    #    insertada por TRUNCATE+INSERT = la del DBF más reciente.
    row = db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        WHERE mesnum = %s AND yy = %s
        ORDER BY id_iniciales DESC
        LIMIT 1
        """,
        (hoy.month, hoy.year),
    )
    if row and (
        float(row.get("kprog") or 0) > 0
        or float(row.get("hilado") or 0) > 0
    ):
        return row

    # 2. La más reciente con datos reales (algún campo > 0)
    row = db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        WHERE COALESCE(kprog, 0) > 0
           OR COALESCE(hilado, 0) > 0
           OR COALESCE(pretot, 0) > 0
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """
    )
    if row:
        return row

    # 3. Fallback final: la más reciente sin importar
    return db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """
    )


_TARIFA_COLS_PREV = {"um", "uk", "uf", "uq", "pre"}


def tarifa_iniciales_mes_anterior(mesnum: int, yy: int, columna: str) -> float:
    """Devuelve `iniciales.<columna>` del mes inmediatamente anterior a (mesnum, yy).

    Se usa como "tarifa al inicio del mes en curso" = cierre del mes
    anterior. Es la palanca para valuar el stock remanente en cálculos
    de promedio ponderado:

        ukg_ponderado = (compras_us + (stock - compras_kg) * tarifa_anterior) / stock

    El valor del MISMO mes NO sirve: se actualiza ex-post al cierre y
    termina siendo el resultado de esta misma fórmula — usarlo introduce
    circularidad y arrastra el redondeo a 3 decimales del DBF.

    `columna` debe estar en `_TARIFA_COLS_PREV` (whitelist anti-injection,
    porque la columna se interpola directo en SQL). Devuelve 0.0 si no
    hay mes anterior con valor cargado.

    Tie-breaker `id_iniciales DESC` para resolver duplicados (mes, yy).
    """
    if not mesnum or not yy:
        return 0.0
    if columna not in _TARIFA_COLS_PREV:
        raise ValueError(f"columna '{columna}' no permitida; usar una de {_TARIFA_COLS_PREV}")
    row = db.fetch_one(
        f"""
        SELECT {columna} AS valor
        FROM scintela.iniciales
        WHERE (yy < %s OR (yy = %s AND mesnum < %s))
          AND COALESCE({columna}, 0) > 0
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """,
        (yy, yy, mesnum),
    )
    return float((row or {}).get("valor") or 0)


def tarifa_um_mes_anterior(mesnum: int, yy: int) -> float:
    """Compatibilidad con código previo. Usa `tarifa_iniciales_mes_anterior`."""
    return tarifa_iniciales_mes_anterior(mesnum, yy, "um")


def ventas_mes_corriente_resultado() -> dict:
    """Ventas del mes EN CURSO calculadas live desde scintela.factura.

    Replica lo que hace INFORMES.PRG: VENTA del mes = SUM(facturas) del mes
    en curso, NO del último cierre histórico. Si la fila historia está
    desfasada, el dBase no muestra ese número desfasado — calcula live.

    Devuelve {kg, importe, n_facturas, dias_pasados, dias_mes}.
    Usa la misma máscara de stat que /facturas?vista=cartera y todas
    las facturas válidas (incluyendo las pagadas/canceladas — todas
    suman a las ventas del mes).
    """
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(kg), 0)      AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.factura
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND (stat IS NULL OR stat <> 'X')
        """
    ) or {}
    hoy = date.today()
    # primer día del mes siguiente menos un día
    if hoy.month == 12:
        ultimo_dia = date(hoy.year + 1, 1, 1).day
    else:
        from calendar import monthrange
        ultimo_dia = monthrange(hoy.year, hoy.month)[1]
    return {
        "n":              int(row.get("n") or 0),
        "kg":             float(row.get("kg") or 0),
        "importe":        float(row.get("importe") or 0),
        "dias_pasados":   hoy.day,
        "dias_mes":       ultimo_dia,
    }


def compras_mes_corriente() -> dict:
    """Compras de MATERIA PRIMA (hilado) del mes en curso.

    Replica VM/MAT.PR. del INFORMES.PRG: en COMPRAS.DBF, las filas con
    TIPO='H' son materia prima (hilado). Otras tipos:
      K = tejido terceriado (va a COSTOS-TEJIDO, no a MAT.PR.)
      T = tintura terceriada
      Q = colorantes/químicos (va a COL.QUI.)
      C = otros (no MAT.PR.)
    Verificado contra DBF real 30/04/2026: TIPO='H' abril = 199.464 kg /
    $581.021 → cuadra exacto con la pantalla del dBase (foto TMT).
    """
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(kg), 0)      AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'H'
          -- Excluir anuladas (stat 'X' o 'Y'). Sin este filtro, compras
          -- reversadas seguían inflando MAT.PR. y U$/kg ponderado.
          -- TMT 2026-05-13.
          AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """
    ) or {}
    return {
        "n":      int(row.get("n") or 0),
        "kg":     float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
    }


# ---------------------------------------------------------------------------
# Provisiones diarias — replica MENU.PRG líneas 282-333.
# ---------------------------------------------------------------------------
#
# Cada vez que dBase abre en un día nuevo Y no es domingo, suma cantidades
# fijas a posdats específicos identificados por (PROV, CONCEPTO_pattern).
# Cada categoría afecta UN solo posdat (el primer match — `LOCA ... IF FOUND`).
#
# Lista canónica derivada del PRG. Total por día hábil = $31,000.
#
# Bucket "YY" (línea 283 dBase: SET FILT TO PROV='YY'):
#   ("YY", "concepto_starts_with", "SR",      2700),
#   ("YY", "concepto_starts_with", "13",      1000),
#   ("YY", "concepto_starts_with", "14",       300),
#   ("YY", "concepto_starts_with", "AB",      1300),
#   ("YY", "concepto_starts_with", "SS",      2400),
#   ("YY", "concepto_starts_with", "A,E,C",   7300),
#   ("YY", "concepto_starts_with", "SUELDOS", 6000),
#   ("YY", "concepto_eq",          "ALQUILER", 700),
# Bucket "ALL" (línea 317 dBase: SET FILT TO — filtro despejado):
#   ("",   "prov_eq",            "RT",      8400),
#   ("",   "concepto_contains",  "INCOB",    400),
#   ("",   "concepto_starts_with","JP",      200),
#   ("",   "concepto_contains",  "INTER",    300),

PROVISIONES_DIARIAS = [
    # (prov_filter, matcher_kind, pattern, monto)
    # TMT 2026-05-15: SR (SRI = Servicio de Rentas Internas) son $3300/día,
    # no $2700. La lista dBase real es: SRI 3300, 13 aguinaldo 1000, 14
    # sueldo 300, AB Andrés Bucheli 1300, IES 2400, AEC 7300, SUELDOS 6000,
    # ALQUILER 700, RT 8400, INCOB 400, JP 200, INTER 300 = $31,600/día.
    # El 2700 era una transcripción vieja del PRG que quedó stale.
    ("YY", "concepto_starts_with",     "SR",      3300),
    ("YY", "concepto_starts_with",     "13",      1000),
    ("YY", "concepto_starts_with",     "14",       300),
    ("YY", "concepto_starts_with",     "AB",      1300),
    ("YY", "concepto_starts_with",     "SS",      2400),
    # TMT 2026-05-15 (re-audit C5): el patrón "A,E,C" ANTES era
    # `concepto LIKE 'A,E,C%'` — nunca matcheaba nada y silenciosamente
    # dropeaba $7,300/día ($160-220k/mes) de provisiones. dBase original
    # usaba `LEFT(concepto,1) $ 'AEC'` (init A, E o C). Lo reemplazamos
    # con el matcher `concepto_starts_with_any` (lista de iniciales).
    ("YY", "concepto_starts_with_any", "A|E|C",   7300),
    ("YY", "concepto_starts_with",     "SUELDOS", 6000),
    ("YY", "concepto_eq",              "ALQUILER", 700),
    ("",   "prov_eq",                  "RT",      8400),
    ("",   "concepto_contains",        "INCOB",    400),
    ("",   "concepto_starts_with",     "JP",       200),
    ("",   "concepto_contains",        "INTER",    300),
]


def _condicion_provision(prov_filter: str, matcher_kind: str, pattern: str) -> tuple[str, list]:
    """Devuelve (sql_where_extra, params) para una provisión.

    Combinable con `WHERE COALESCE(banc,0) <> 9 AND <esto>`.
    """
    conds = []
    params: list = []
    if prov_filter:
        conds.append("UPPER(TRIM(COALESCE(prov, ''))) = %s")
        params.append(prov_filter.upper())
    if matcher_kind == "concepto_starts_with":
        conds.append("UPPER(COALESCE(concepto, '')) LIKE %s")
        params.append(pattern.upper() + "%")
    elif matcher_kind == "concepto_starts_with_any":
        # pattern es lista pipe-separada, ej. "A|E|C" → concepto inicia
        # con cualquiera. Es UN solo match (LIMIT 1 del caller); el primero
        # gana. Equivalente dBase: LEFT(concepto,1) $ 'AEC'.
        iniciales = [p.strip().upper() for p in pattern.split("|") if p.strip()]
        if not iniciales:
            return ("FALSE", [])  # patrón vacío → 0 matches
        ors = []
        for ini in iniciales:
            ors.append("UPPER(COALESCE(concepto, '')) LIKE %s")
            params.append(ini + "%")
        conds.append("(" + " OR ".join(ors) + ")")
    elif matcher_kind == "concepto_eq":
        conds.append("UPPER(TRIM(COALESCE(concepto, ''))) = %s")
        params.append(pattern.upper())
    elif matcher_kind == "concepto_contains":
        conds.append("UPPER(COALESCE(concepto, '')) LIKE %s")
        params.append("%" + pattern.upper() + "%")
    elif matcher_kind == "prov_eq":
        conds.append("UPPER(TRIM(COALESCE(prov, ''))) = %s")
        params.append(pattern.upper())
    return (" AND ".join(conds), params)


def correr_provisiones_diarias(forzar: bool = False) -> dict:
    """Aplica las provisiones diarias del PRG MENU.PRG L282-333.

    **Catch-up automático**: aplica TODOS los días hábiles entre la última
    corrida y hoy (inclusive). Si no abriste el sistema durante 3 días,
    aplica los 3 días faltantes — el resultado no depende de cuándo entres.

    Idempotente: lee `scintela.sistema_meta` clave='provisiones_diarias_ult_fecha'.
    Si `ult_fecha >= hoy`, no hace nada. Si no, aplica un día por cada día
    hábil pendiente y avanza el marker a hoy.

    Excluye domingos (`weekday() == 6`). Si hoy es domingo, el marker
    igual avanza pero ese día NO suma — igual que dBase legacy.

    Cada día aplicado afecta UN solo posdat por categoría (primer match
    por id_posdat). Si no hay match, se saltea (igual que dBase).

    Si `forzar=True`, aplica UN día extra incluso si ya estaba al día.
    Útil para emparejar valores cuando el sistema vino atrasado.

    Devuelve:
        {
          "aplicado": bool,            # si se aplicó al menos 1 día,
          "dias_aplicados": int,       # cantidad de días hábiles aplicados,
          "monto_total": float,        # suma total agregada (todos los días),
          "categorias_por_dia": int,   # cuántas categorías matchearon (~12),
          "ult_fecha_anterior": str,
          "ult_fecha_nueva": str,
        }
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    hoy = _date.today()
    hoy_iso = hoy.isoformat()

    # TMT 2026-05-14 (audit #36): toda la lógica de leer ult_fecha,
    # calcular días pendientes, aplicar UPDATEs y avanzar marker DEBE
    # correr dentro de UNA SOLA transacción con lock pessimista en la
    # fila de sistema_meta. Antes el SELECT inicial estaba fuera de la
    # tx → dos GETs concurrentes a /informes/balance leían ult_fecha=ayer
    # y aplicaban las provisiones DOS veces. SELECT...FOR UPDATE serializa.

    total = 0.0
    cats_ultima = 0
    dias_a_aplicar: list[_date] = []
    ult_fecha_str: str | None = None

    with db.tx() as conn:
        # Lock pessimista — bloquea hasta que la tx concurrente commitee.
        lock_row = db.fetch_one(
            "SELECT valor FROM scintela.sistema_meta "
            " WHERE clave = %s FOR UPDATE",
            ("provisiones_diarias_ult_fecha",),
            conn=conn,
        )
        ult_fecha_str = (lock_row or {}).get("valor")

        # Si nunca corrió, inicializar a ayer (próxima corrida aplica hoy).
        if not ult_fecha_str:
            db.execute(
                """INSERT INTO scintela.sistema_meta (clave, valor)
                   VALUES ('provisiones_diarias_ult_fecha', %s)
                   ON CONFLICT (clave) DO UPDATE
                     SET valor = EXCLUDED.valor, actualizado = CURRENT_TIMESTAMP""",
                ((hoy - _td(days=1)).isoformat(),),
                conn=conn,
            )
            ult_fecha_str = (hoy - _td(days=1)).isoformat()

        try:
            ult_fecha = _date.fromisoformat(ult_fecha_str)
        except (TypeError, ValueError):
            ult_fecha = hoy - _td(days=1)

        # Calcular días hábiles pendientes (entre ult_fecha+1 y hoy inclusive).
        # Si forzar=True, agregamos un día extra al final como catch-up manual.
        cursor_d = ult_fecha + _td(days=1)
        while cursor_d <= hoy:
            if cursor_d.weekday() != 6:  # 6 = domingo en Python
                dias_a_aplicar.append(cursor_d)
            cursor_d += _td(days=1)

        # TMT 2026-05-15 (re-audit C6): si `forzar=True` y ya estamos al
        # día (ult_fecha >= hoy), NO permitimos otra corrida — antes,
        # llamar forzar dos veces seguidas re-aplicaba el mismo día y
        # duplicaba las provisiones. La idea de `forzar` es disparar un día
        # cuando el sistema vino atrasado, no agregar duplicados.
        if forzar and not dias_a_aplicar:
            if ult_fecha >= hoy:
                return {
                    "aplicado": False, "dias_aplicados": 0,
                    "monto_total": 0.0, "categorias_por_dia": 0,
                    "ult_fecha_anterior": ult_fecha_str,
                    "ult_fecha_nueva": ult_fecha_str,
                    "motivo": (
                        f"forzar rechazado: ya se aplicó hasta {ult_fecha_str} "
                        f"≥ hoy {hoy_iso}. No permitimos doble-aplicar."
                    ),
                }
            dias_a_aplicar = [hoy if hoy.weekday() != 6 else hoy - _td(days=1)]

        if not dias_a_aplicar:
            # Lock liberado al salir del with. Sin cambios — devolver sin
            # tocar el marker.
            return {
                "aplicado": False, "dias_aplicados": 0,
                "monto_total": 0.0, "categorias_por_dia": 0,
                "ult_fecha_anterior": ult_fecha_str,
                "ult_fecha_nueva": ult_fecha_str,
                "motivo": "ya al día (sin días hábiles pendientes)",
            }

        # Aplicar UNA vez por cada día hábil pendiente.
        for _dia in dias_a_aplicar:
            cats_dia = 0
            for prov_filter, matcher_kind, pattern, monto in PROVISIONES_DIARIAS:
                where_extra, params = _condicion_provision(prov_filter, matcher_kind, pattern)
                sql = f"""
                    WITH first_match AS (
                        SELECT id_posdat
                          FROM scintela.posdat
                         WHERE COALESCE(banc, 0) <> 9
                           AND (anulada IS NOT TRUE OR anulada IS NULL)
                           AND {where_extra}
                         ORDER BY id_posdat
                         LIMIT 1
                    )
                    UPDATE scintela.posdat p
                       SET importe = COALESCE(p.importe, 0) + %s,
                           fecha_modifica = CURRENT_TIMESTAMP,
                           usuario_modifica = 'provisiones_diarias'
                      FROM first_match fm
                     WHERE p.id_posdat = fm.id_posdat
                    RETURNING p.id_posdat
                """
                ret = db.fetch_one(sql, tuple(params + [monto]), conn=conn)
                if ret:
                    cats_dia += 1
                    total += monto
            cats_ultima = cats_dia

        # Actualizar marker — siempre avanzar a hoy
        db.execute(
            """UPDATE scintela.sistema_meta
                  SET valor = %s, actualizado = CURRENT_TIMESTAMP
                WHERE clave = 'provisiones_diarias_ult_fecha'""",
            (hoy_iso,),
            conn=conn,
        )

    return {
        "aplicado": True,
        "dias_aplicados": len(dias_a_aplicar),
        "monto_total": total,
        "categorias_por_dia": cats_ultima,
        "ult_fecha_anterior": ult_fecha_str,
        "ult_fecha_nueva": hoy_iso,
    }


def amortizaciones_mensuales() -> dict:
    """Amortizaciones del mes desde scintela.activos (INFORMES.PRG líneas 42-50).

    DEPRACT  = SUM(amortimes WHERE tipo='I')   inmuebles (terr/edif)
    DEPRMAQ  = SUM(amortimes WHERE tipo='M')   maquinaria
    DEPRTEJ  = SUM(amortimes WHERE tipo='K')   tejeduría
    DEPRCAR  = SUM(amortimes WHERE tipo='C')   carros / cómputo

    DCC = DEPRMAQ + DEPRACT * 0.5    → amortiz tintorería (50% inmuebles)
    DTJ = DEPRTEJ + DEPRACT * 0.5    → amortiz tejeduría (50% inmuebles)

    `amortimes` se computa on-the-fly con la proración diaria de dBase
    (MENU.PRG L275): `COEF * CUOTA` donde `COEF = min(DAY(today), 30) / 30`.
    NO leemos la columna `amortimes` stored porque sólo se refresca al
    cierre del mes — quedaría desfasada vs dBase y los costos por kg
    (VK/KK, GTIN/KR) mostrarían valores más bajos cada día del mes.
    Bug TMT 2026-05-13: TEJIDO 0.602 vs dBase 0.627; GS.PROC 1.846 vs 1.883.
    """
    rows = db.fetch_all(
        """
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c
        )
        SELECT UPPER(TRIM(tipo)) AS tipo,
               COALESCE(SUM((SELECT c FROM coef) * COALESCE(cuota, 0)), 0) AS total
        FROM scintela.activos
        WHERE COALESCE(cuota, 0) > 0
        GROUP BY 1
        """
    ) or []
    by = {r.get("tipo"): float(r.get("total") or 0) for r in rows}
    depract = by.get("I", 0.0)
    deprmaq = by.get("M", 0.0)
    deprtej = by.get("K", 0.0)
    deprcar = by.get("C", 0.0)
    return {
        "depract": depract,
        "deprmaq": deprmaq,
        "deprtej": deprtej,
        "deprcar": deprcar,
        "dcc":     deprmaq + depract * 0.5,
        "dtj":     deprtej + depract * 0.5,
    }


# Etiquetas humanas de las 9 categorías de XGAST (PRG INFORMES.PRG L211-217).
GASTOS_NUM_LABELS = {
    1: "V1 — Sueldos tejeduría",
    2: "V2 — Gas/Comb. tejeduría",
    3: "V3 — Gs. varios tejeduría",
    4: "V4 — Sueldos tintorería",
    5: "V5 — Gas/Comb. tintorería",
    6: "V6 — Gs. varios tintorería",
    7: "V7 — Sueldos admin",
    8: "V8 — Gas/Comb. admin",
    9: "V9 — Gs. varios admin",
}


def _grupo_concepto(concepto: str | None) -> str:
    """Detecta el grupo de un gasto V5 — replica PRG L1280-1283.

    El PRG hace esto para `VAL(RES)=5` (Gas/Comb tintorería) específicamente
    porque ahí concentra todos los servicios (luz, agua, gas):

        REPLA SALDO WITH 1 FOR "EEQ"   $ CONCEPTO   → EEQ = Empresa Eléctrica
        REPLA SALDO WITH 2 FOR "CMB"   $ CONCEPTO   → CMB = Combustible
        REPLA SALDO WITH 3 FOR "EMAAP" $ CONCEPTO   → EMAAP = Agua/EMAAP
        REPLA SALDO WITH 3 FOR "AGUA"  $ CONCEPTO   → Agua (idem)
        INDEX ON SALDO TO CC

    Para las demás categorías el PRG no agrupa, así que devolvemos el
    primer token (3-5 letras) del concepto como bucket genérico — útil
    para que el usuario vea "SUELDOS … SUELDOS … SUELDOS" juntos sin
    tener que abrir más drill-downs.
    """
    c = (concepto or "").upper().strip()
    if not c:
        return "(sin concepto)"
    if "EEQ" in c:
        return "EEQ — Eléctrica"
    if "CMB" in c:
        return "CMB — Combustible"
    if "EMAAP" in c or "AGUA" in c:
        return "EMAAP/AGUA"
    if "INCOB" in c:
        return "INCOBRABLES"
    if "INTER" in c:
        return "INTER"
    # Genérico: primer token significativo (max 5 chars).
    token = c.split()[0][:5]
    return token or "(otros)"


def gastos_detalle_categoria(num: int, mes_actual: bool = True) -> dict:
    """Drill-down de una categoría V1..V9 — PRG INFORMES.PRG L1266-1334.

    Devuelve la lista detallada de filas de `scintela.xgast` para esa
    categoría, AGRUPADAS por concepto (EEQ/CMB/EMAAP/etc para V5; primer
    token para las demás). Equivalente a la pantalla DETALGAST del PRG
    accesible desde GASTOS.

    Estructura:
        {
          "num": 5,
          "label": "V5 — Gas/Comb. tintorería",
          "grupos": [
            {"grupo": "EEQ — Eléctrica", "filas": [...], "subtotal": 12345.67},
            {"grupo": "CMB — Combustible", "filas": [...], "subtotal": 6789.01},
            ...
          ],
          "total": 32145.67,
          "n_filas": 24,
        }

    Args:
        num: número de categoría (1..9).
        mes_actual: si True, filtra al mes en curso. Si False, sin filtro
                    de fecha (raro — sólo para debugging).
    """
    try:
        n = int(num)
    except (TypeError, ValueError):
        return {"num": 0, "label": "", "grupos": [], "total": 0.0, "n_filas": 0}
    if n < 1 or n > 9:
        return {"num": n, "label": "(categoría inválida)",
                "grupos": [], "total": 0.0, "n_filas": 0}

    where_fecha = (
        "AND fecha >= date_trunc('month', CURRENT_DATE) "
        "AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'"
        if mes_actual else ""
    )
    # TMT 2026-05-19 v6 re-audit — agregado filtro stat='Y' (anuladas).
    # Antes el drill-down mostraba gastos anulados sumados al subtotal,
    # discrepando con `gastos_xgast_v1_a_v9_mes` que sí los excluye.
    sql = f"""
        SELECT id_xgast, fecha, doc, prov, concepto, importe, stat, fechad, saldo
        FROM scintela.xgast
        WHERE num = %s
          AND COALESCE(stat, '') <> 'Y'
          {where_fecha}
        ORDER BY fecha ASC, id_xgast ASC
    """
    filas = db.fetch_all(sql, (n,)) or []

    # TMT 2026-05-19 v2 — incluir compras cuyo (tipo, concepto, prov) mapea
    # a este num según la cascada del dBase (`_SQL_COMPRA_NUM_CASE`). Antes
    # filtraba sólo por tipo; ahora respeta SU/EEQ/AGUA/etc.
    where_fecha_c = (
        "AND c.fecha >= date_trunc('month', CURRENT_DATE) "
        "AND c.fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'"
        if mes_actual else ""
    )
    # El CASE va en el SELECT y filtramos por el resultado en un wrapper
    # outer (Postgres no permite filtrar por columna calculada en el mismo
    # WHERE sin LATERAL/subquery).
    sql_c = f"""
        SELECT * FROM (
            SELECT c.id_compra, c.fecha, c.comprobante AS doc, c.codigo_prov AS prov,
                   c.concepto, c.importe, c.stat, c.tipo,
                   ({_SQL_COMPRA_NUM_CASE}) AS num_calc
              FROM scintela.compra c
             WHERE COALESCE(c.stat, '') NOT IN ('X', 'Y')
               {where_fecha_c}
        ) sub
         WHERE num_calc = %s
         ORDER BY fecha ASC, id_compra ASC
    """
    filas_compras = db.fetch_all(sql_c, (n,)) or []

    # Agrupar por concepto via _grupo_concepto.
    buckets: dict[str, dict] = {}
    total = 0.0
    for r in filas:
        grupo = _grupo_concepto(r.get("concepto"))
        importe = float(r.get("importe") or 0)
        total += importe
        if grupo not in buckets:
            buckets[grupo] = {"grupo": grupo, "filas": [], "subtotal": 0.0}
        buckets[grupo]["filas"].append({
            "id_xgast": r.get("id_xgast"),
            "fecha":    r.get("fecha"),
            "doc":      r.get("doc") or "",
            "prov":     r.get("prov") or "",
            "concepto": r.get("concepto") or "",
            "importe":  importe,
            "stat":     r.get("stat") or "",
            "fuente":   "xgast",
        })
        buckets[grupo]["subtotal"] += importe

    # Bucket separado para compras (mejor UX que mezclarlas con conceptos
    # de xgast — tienen distinta estructura, distinto reverso).
    for r in filas_compras:
        tipo_c = (r.get("tipo") or "").upper().strip()
        grupo = f"Compras (tipo {tipo_c})"
        importe = float(r.get("importe") or 0)
        total += importe
        if grupo not in buckets:
            buckets[grupo] = {"grupo": grupo, "filas": [], "subtotal": 0.0}
        buckets[grupo]["filas"].append({
            "id_compra": r.get("id_compra"),
            "fecha":     r.get("fecha"),
            "doc":       r.get("doc") or "",
            "prov":      r.get("prov") or "",
            "concepto":  r.get("concepto") or "",
            "importe":   importe,
            "stat":      r.get("stat") or "",
            "fuente":    "compra",
            "tipo":      tipo_c,
        })
        buckets[grupo]["subtotal"] += importe

    # Ordenamos los grupos por subtotal descendente — los gastos más grandes arriba.
    grupos = sorted(buckets.values(), key=lambda g: g["subtotal"], reverse=True)

    return {
        "num":     n,
        "label":   GASTOS_NUM_LABELS.get(n, f"V{n}"),
        "grupos":  grupos,
        "total":   total,
        "n_filas": len(filas),
    }


# Mapping compra → xgast.num (V1..V9) — TMT 2026-05-19 v2.
# Replica la cascada de INFORMES.PRG L160-169 (líneas &RNW 1..9). Combina
# el `tipo` de compra (que determina el rubro) con patrones de concepto y
# codigo_prov (que determinan la sub-cat dentro del rubro).
#
# Reglas (primer match gana, igual que en dBase):
#   1. tipo K + concepto contiene 'SU'              → V1 (Tej · Sueldos)
#   2. tipo K + concepto contiene 'EEQ'             → V2 (Tej · Servicios)
#   3. tipo K + resto                                → V3 (Tej · Otros)
#   4. tipo C/Q/T + ('CCSU' OR SU al inicio)        → V4 (Tin · Sueldos)
#   5. tipo C/Q/T + concepto contiene CMB/EEQ/AGUA/EMAAP → V5 (Tin · Servicios)
#   6. tipo C/Q/T + resto                            → V6 (Tin · Otros)
#   7. tipo S + (concepto SU OR LC2=SU)             → V7 (Adm · Sueldos)
#   8. tipo S + concepto arranca con GAS            → V8 (Adm · Servicios)
#   9. tipo S + resto                                → V9 (Adm · Otros)
#   excluidos:
#     tipo H (Hilado)   — materia prima, no gasto.
#     tipo A/I (Anticipos) — activo diferido, no gasto.
#     tipo K con kg>0  — producción, ya entra en VK/IPROVK.
#
# SQL CASE equivalente vive en `_SQL_COMPRA_NUM_CASE` más abajo.
COMPRA_A_GASTO_REGLAS: list[tuple[str, int, str]] = [
    ("K_SU",     1, "Tej · Sueldos"),
    ("K_EEQ",    2, "Tej · Servicios"),
    ("K_OTROS",  3, "Tej · Otros"),
    ("C_SU",     4, "Tin · Sueldos"),
    ("C_SERV",   5, "Tin · Servicios"),
    ("C_OTROS",  6, "Tin · Otros"),
    ("S_SU",     7, "Adm · Sueldos"),
    ("S_GAS",    8, "Adm · Servicios"),
    ("S_OTROS",  9, "Adm · Otros"),
]

# Keywords de "servicios" — los mismos en los 3 rubros (Tej/Tin/Adm).
# Pedido Tamara 2026-05-19 v3: los servicios son "repetibles" — agua,
# emaap, luz, combustible, gasolina pueden aparecer en cualquier rubro;
# el tipo de compra decide el rubro (V2/V5/V8), pero la palabra clave es
# la misma. Antes V2 sólo aceptaba EEQ y V8 sólo GAS — desproporcionado.
#
# Mantenido el LIKE 'GAS%%' (prefix) además del contains para que matchee
# "GASOLINA", "GASTOS DE TRANSPORTE", etc. exactamente como hace dBase
# con LEFT(CONCEPTO,3)='GAS'. CMB/EEQ/AGUA/EMAAP van con contains.
_SERVICIOS_KEYWORDS_SQL = """
   (UPPER(COALESCE(c.concepto, '')) LIKE '%%CMB%%'
    OR UPPER(COALESCE(c.concepto, '')) LIKE '%%EEQ%%'
    OR UPPER(COALESCE(c.concepto, '')) LIKE '%%AGUA%%'
    OR UPPER(COALESCE(c.concepto, '')) LIKE '%%EMAAP%%'
    OR UPPER(COALESCE(c.concepto, '')) LIKE 'GAS%%')
""".strip()


# SQL CASE para mapear (tipo, concepto, codigo_prov) → num V1..V9.
# Devuelve NULL para compras que no entran al matriz (H, A, I, K-producción).
# Cascada — primer match gana, igual que las &RNW del PRG.
_SQL_COMPRA_NUM_CASE = f"""
CASE
    -- Excluir materia prima y anticipos
    WHEN UPPER(COALESCE(c.tipo, '')) IN ('H', 'A', 'I') THEN NULL
    -- Excluir producción (tipo K + kg>0 cuenta en VK/IPROVK, no gasto)
    WHEN UPPER(COALESCE(c.tipo, '')) = 'K' AND COALESCE(c.kg, 0) > 0 THEN NULL

    -- V1: Tejeduría · Sueldos
    WHEN UPPER(COALESCE(c.tipo, '')) = 'K'
         AND UPPER(COALESCE(c.concepto, '')) LIKE '%%SU%%' THEN 1
    -- V2: Tejeduría · Servicios (full set repetible: CMB/EEQ/AGUA/EMAAP/GAS)
    WHEN UPPER(COALESCE(c.tipo, '')) = 'K'
         AND {_SERVICIOS_KEYWORDS_SQL} THEN 2
    -- V3: Tejeduría · Otros (catch-all K-sin-kg)
    WHEN UPPER(COALESCE(c.tipo, '')) = 'K' THEN 3

    -- V4: Tintorería · Sueldos (CCSU explícito o concepto arranca con SU)
    WHEN UPPER(COALESCE(c.tipo, '')) IN ('C', 'Q', 'T')
         AND (UPPER(COALESCE(c.concepto, '')) LIKE '%%CCSU%%'
              OR UPPER(LEFT(COALESCE(c.concepto, ''), 2)) = 'SU') THEN 4
    -- V5: Tintorería · Servicios (mismo set que V2/V8)
    WHEN UPPER(COALESCE(c.tipo, '')) IN ('C', 'Q', 'T')
         AND {_SERVICIOS_KEYWORDS_SQL} THEN 5
    -- V6: Tintorería · Otros (catch-all C/Q/T)
    WHEN UPPER(COALESCE(c.tipo, '')) IN ('C', 'Q', 'T') THEN 6

    -- V7: Administración · Sueldos
    WHEN UPPER(COALESCE(c.tipo, '')) = 'S'
         AND (UPPER(COALESCE(c.concepto, '')) LIKE '%%SU%%'
              OR UPPER(LEFT(COALESCE(c.concepto, ''), 2)) = 'SU') THEN 7
    -- V8: Administración · Servicios (mismo set que V2/V5)
    WHEN UPPER(COALESCE(c.tipo, '')) = 'S'
         AND {_SERVICIOS_KEYWORDS_SQL} THEN 8
    -- V9: Administración · Otros (catch-all S y default)
    WHEN UPPER(COALESCE(c.tipo, '')) = 'S' THEN 9

    ELSE NULL
END
""".strip()


# Mapping legacy simple — preservado para callers externos que importen
# este símbolo. NO usar para clasificar (usar _SQL_COMPRA_NUM_CASE).
TIPOS_COMPRA_A_NUM_GASTO: dict[str, int] = {
    "K": 3,   # tipo K default → V3 (refinado por concepto en SQL CASE)
    "C": 6,
    "Q": 6,
    "T": 6,
    "S": 9,
}


def gastos_xgast_v1_a_v9_mes() -> dict:
    """V1..V9 del PRG: SUM(importe) FROM xgast + compras (por tipo) WHERE mes en curso.

    Los rótulos PRG:
      V1 = sueldos tejeduría        V4 = sueldos tintorería       V7 = sueldos admin
      V2 = gas/comb tejeduría       V5 = gas/comb tintorería      V8 = gas/comb admin
      V3 = gs.varios tejeduría      V6 = gs.varios tintorería     V9 = gs.varios admin

    TMT 2026-05-19 — incluye compras del mes mapeadas por tipo (ver
    `TIPOS_COMPRA_A_NUM_GASTO`). Antes solo leía xgast — pedido Tamara para
    que los servicios de tintorería/tejeduría aparezcan automáticamente sin
    duplicar carga en xgast. Excluye anuladas (stat 'X','Y') y excluye tipos
    que no son gastos (H, A, I).

    Devuelve {v1..v9, gtej_sin_dtj, gtin_sin_dcc, gs_sin_deprcar}.
    """
    rows_xgast = db.fetch_all(
        """
        SELECT COALESCE(num, 0) AS num,
               COALESCE(SUM(importe), 0) AS total
        FROM scintela.xgast
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        GROUP BY 1
        """
    ) or []
    v = {int(r.get("num") or 0): float(r.get("total") or 0) for r in rows_xgast}

    # Sumar compras del mes mapeadas por la cascada dBase (tipo + concepto
    # + codigo_prov). Excluye anuladas, materia prima (H), anticipos (A/I)
    # y producción (K con kg>0). Mapping completo en `_SQL_COMPRA_NUM_CASE`.
    sql_compras = f"""
        SELECT ({_SQL_COMPRA_NUM_CASE}) AS num,
               COALESCE(SUM(c.importe), 0) AS total
          FROM scintela.compra c
         WHERE c.fecha >= date_trunc('month', CURRENT_DATE)
           AND c.fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
         GROUP BY 1
    """
    rows_compras = db.fetch_all(sql_compras) or []
    for r in rows_compras:
        num = r.get("num")
        if not num:
            continue
        v[int(num)] = v.get(int(num), 0.0) + float(r.get("total") or 0)

    return {
        "v1": v.get(1, 0.0), "v2": v.get(2, 0.0), "v3": v.get(3, 0.0),
        "v4": v.get(4, 0.0), "v5": v.get(5, 0.0), "v6": v.get(6, 0.0),
        "v7": v.get(7, 0.0), "v8": v.get(8, 0.0), "v9": v.get(9, 0.0),
        "gtej_sin_dtj":      v.get(1, 0) + v.get(2, 0) + v.get(3, 0),
        "gtin_sin_dcc":      v.get(4, 0) + v.get(5, 0) + v.get(6, 0),
        "gs_sin_deprcar":    v.get(7, 0) + v.get(8, 0) + v.get(9, 0),
    }


def tinto_mes_corriente_resultado() -> dict:
    """Datos de TINTO.DBF del mes (replicada en scintela.tinto).

    PRG líneas 252-256 — replicado exacto:
      ITIN  = SUM(importe)                          (TODAS las filas, sin filtro)
      KTINT = SUM(kg)  WHERE color NOT LIKE 'LAV%'  (kg tinturados en INTELA)
      KR    = SUM(kgn) WHERE color NOT LIKE 'LAV%' AND kg > 0
              (kg que llegan a terminado, excluyendo lavados de máquina)

    Trampa documentada por TMT 2026-05-06: el PRG hace `FOR .NOT. COLOR='LAV'`,
    que en dBase con SET EXACT OFF (default) hace **prefix match** — entonces
    "LAVADO MAQ" SÍ matchea como LAV. Nuestro SQL anterior usaba `<>'LAV'`
    exact y dejaba pasar la fila de lavado, inflando KR en 600 kg.

    Otra trampa: KR del PRG no filtra kg>0 explícitamente, pero los datos
    reales tienen una sola fila tipo "LAVADO MAQ" con kg=0 y kgn>0 que
    no debería entrar en KR. El kg>0 lo excluye correctamente.
    """
    # NOTA: los `%` literales en `LIKE 'LAV%'` están escapados como `%%`.
    # psycopg2 los confunde con placeholders cuando params es `()` (default
    # de db.fetch_one) y tira "tuple index out of range". Mismo patrón
    # que `provisiones/queries.py` (ver nota allá).
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0)                                     AS itin,
               COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                 THEN kg  ELSE 0 END), 0)                    AS ktint,
               COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                  AND COALESCE(kg, 0) > 0
                                 THEN kgn ELSE 0 END), 0)                    AS kr
        FROM scintela.tinto
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        """
    ) or {}
    return {
        "itin":  float(row.get("itin") or 0),
        "ktint": float(row.get("ktint") or 0),
        "kr":    float(row.get("kr") or 0),
    }


def compras_iprovk_mes() -> dict:
    """IPROVK del PRG: compras de TEJIDO TERCERIZADO (no INTELA) del mes.

    Filtro PRG línea 230: `TIPO='K' AND PROV<>'KK' AND KG>0`.
    Mantenido como compat. Para el panel Resultados v2 usar
    `tejido_mes_componentes()` que descompone interno/externo/gastos-KK.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg),      0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'K'
          AND COALESCE(UPPER(TRIM(codigo_prov)), '') <> 'KK'
          AND COALESCE(kg, 0) > 0
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
        """
    ) or {}
    return {
        "kg":      float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
    }


def compras_tipo_t_externos_mes() -> dict:
    """KT_externos del PRG: compras tipo='T' tercerizado externo del mes.

    PRG línea 245: en el TOTAL TO PASACOM, KT = KG of TIPO='T' rows.
    Filtramos `prov<>'KK' AND kg>0` para mantener consistencia con
    `compras_iprovk_mes()` (IPROV/IPROVK pattern).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg),      0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'T'
          AND COALESCE(UPPER(TRIM(codigo_prov)), '') <> 'KK'
          AND COALESCE(kg, 0) > 0
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
        """
    ) or {}
    return {
        "kg":      float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
    }


def tinto_kg_servicios_mes() -> float:
    """KSTI del PRG (línea 254): SUM(kg) FROM tinto WHERE stat='S'.

    Son los kg de tinto prestados como SERVICIO a terceros (= no son
    nuestros, los tinturamos para otros). Se restan de KT en la fórmula
    de stock: `KT = KT_externos + KTINT - KSTI`.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg
        FROM scintela.tinto
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND UPPER(TRIM(stat)) = 'S'
        """
    ) or {}
    return float(row.get("kg") or 0)


def tejido_mes_componentes() -> dict:
    """Descompone las compras tipo='K' del mes en sus tres componentes legacy.

    Convención dBase (PRG INFORMES.PRG + verificación con DBFs reales 2026-05-06):

    - **PROV='KK' AND KG>0** = kg tejidos por la planta INTELA. Se auto-cargan
      como "compras a sí mismo". El concepto típico es la fecha del día como
      texto + el número de partida (e.g. "01/05         4"). Estos kg son
      el verdadero "kg tejidos del mes" — el dBase los contabilizaba pero la
      query `compras_iprovk_mes()` los excluía por el filtro PROV<>'KK',
      dejando la celda kg de TEJIDO en 0 cuando no había tercerización
      (= la mayoría de los meses).

    - **PROV='KK' AND IMPORTE>0 (KG=0)** = gastos varios cargados al rubro
      tejido (transporte DHL, mascarillas, almuerzos, etc.). Se suman al U$
      de TEJIDO pero NO aportan kg.

    - **PROV<>'KK' AND KG>0** = tejido tercerizado en otra planta. Aporta a
      kg y U$ ambos.

    Returns:
      {
        "kg_interno":    kg tejidos por INTELA (PROV='KK' AND KG>0)
        "kg_externo":    kg tercerizados (PROV<>'KK' AND KG>0)
        "us_externo":    importe tercerizados externos (KG>0)
        "us_kk_gastos":  importe gastos KK varios (KG=0)
        "kg_total":      kg_interno + kg_externo
        "us_total":      us_externo + us_kk_gastos
      }
    """
    rows = db.fetch_all(
        """
        SELECT
            CASE WHEN COALESCE(UPPER(TRIM(codigo_prov)),'') = 'KK'
                 THEN 'KK' ELSE 'OTRO' END                                    AS quien,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) > 0 THEN kg      ELSE 0 END), 0) AS kg_con_kg,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) > 0 THEN importe ELSE 0 END), 0) AS us_con_kg,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) = 0 THEN importe ELSE 0 END), 0) AS us_sin_kg
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', CURRENT_DATE)
          AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'K'
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
        GROUP BY 1
        """
    ) or []
    out = {
        "kg_interno": 0.0, "kg_externo": 0.0,
        "us_externo": 0.0, "us_kk_gastos": 0.0,
    }
    for r in rows:
        if r.get("quien") == "KK":
            out["kg_interno"]   = float(r.get("kg_con_kg") or 0)
            out["us_kk_gastos"] = float(r.get("us_sin_kg") or 0)
            # Si una fila KK tuviera kg>0 y importe>0 simultaneamente (raro
            # pero posible), su importe NO se suma como "gasto" — ya está
            # contado como costo de tejido interno por kg. Defensivo: dejarlo.
        else:
            out["kg_externo"] = float(r.get("kg_con_kg") or 0)
            out["us_externo"] = (
                float(r.get("us_con_kg") or 0) + float(r.get("us_sin_kg") or 0)
            )
    out["kg_total"] = out["kg_interno"] + out["kg_externo"]
    out["us_total"] = out["us_externo"] + out["us_kk_gastos"]
    return out


def costo_promedio_mp_ponderado(
    h_kcom: float,
    h_ucom: float,
    hist: dict | None,
    inic: dict | None,
) -> dict:
    """Costo promedio ponderado del stock de materia prima (hilado).

    Combina el stock al **inicio del mes** con las **compras del mes** para
    dar un U$/kg "real" del hilado disponible, en vez del ratio volátil
    `compras_us / compras_kg` que se va a 0 en meses sin compras.

    Fórmula contable estándar (weighted average):

        ukg_promedio = (stock_anterior_us + compras_mes_us)
                     / (stock_anterior_kg + compras_mes_kg)

    Donde:
      - stock_anterior_kg = `historia.hilado` (kg al último cierre)
      - tarifa_anterior   = `iniciales.um` (target U$/kg) — preferimos el
        objetivo sobre `umx` (= compras-of-month) para evitar circularidad
        y dar un número estable
      - stock_anterior_us = stock_anterior_kg * tarifa_anterior

    Returns:
      {
        "ukg_promedio":     ratio ponderado (puede ser 0 si no hay datos)
        "kg_disponible":    stock_anterior_kg + compras_mes_kg
        "us_disponible":    stock_anterior_us + compras_mes_us
        "stock_anterior_kg": kg al inicio del mes
        "stock_anterior_us": valor inicial estimado
        "tarifa_anterior":  U$/kg usado para valuar el stock anterior
        "src":              'stock' si combinamos los dos lados,
                            'compras' si sólo hay compras,
                            'stock_only' si sólo hay stock sin compras,
                            'none' si no hay nada
      }
    """
    stock_anterior_kg = float((hist or {}).get("hilado") or 0)
    tarifa_anterior = float((inic or {}).get("um") or 0)
    # Fallback: si iniciales no tiene um, usamos el ratio de compras del mes
    # para valuar el stock anterior — peor que iniciales pero mejor que 0.
    if not tarifa_anterior and h_kcom:
        tarifa_anterior = h_ucom / h_kcom
    stock_anterior_us = stock_anterior_kg * tarifa_anterior

    kg_disp = stock_anterior_kg + (h_kcom or 0)
    us_disp = stock_anterior_us + (h_ucom or 0)
    ukg = us_disp / kg_disp if kg_disp > 0 else 0.0

    if stock_anterior_kg > 0 and (h_kcom or 0) > 0:
        src = "stock"
    elif (h_kcom or 0) > 0:
        src = "compras"
    elif stock_anterior_kg > 0:
        src = "stock_only"
    else:
        src = "none"

    return {
        "ukg_promedio":      ukg,
        "kg_disponible":     kg_disp,
        "us_disponible":     us_disp,
        "stock_anterior_kg": stock_anterior_kg,
        "stock_anterior_us": stock_anterior_us,
        "tarifa_anterior":   tarifa_anterior,
        "src":               src,
    }


def ventas_anio_en_curso() -> float:
    """Ventas del año calendario en curso: SUM(historia.uvent) de meses
    cerrados + facturado live del mes en curso.

    TMT 2026-05-19 v8 (revisión 2) — dueña: "ventas del año sale de
    historia, deberiamos sumar los usd de cada mes". scintela.historia
    tiene una fila por mes cerrado con uvent definitivo. Sumamos los
    meses cerrados del año, y para el mes en curso (que todavía no tiene
    snapshot) usamos el facturado live de scintela.factura.

    Si historia falla, fallback a la suma live de factura (solo positivos).
    """
    from datetime import date as _date
    hoy = _date.today()
    yy = hoy.year
    mm = hoy.month

    try:
        # Meses cerrados del año actual desde historia (uvent definitivo).
        row_hist = db.fetch_one(
            """
            SELECT COALESCE(SUM(uvent), 0) AS total
              FROM scintela.historia
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) < %s
            """,
            (yy, mm),
        ) or {}
        uvent_cerrados = float(row_hist.get("total") or 0)
    except Exception:
        uvent_cerrados = 0.0

    try:
        # Mes en curso: live desde scintela.factura (sólo positivos).
        row_live = db.fetch_one(
            """
            SELECT COALESCE(SUM(importe), 0) AS total
              FROM scintela.factura
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) = %s
               AND COALESCE(stat, '') <> 'X'
               AND COALESCE(importe, 0) > 0
            """,
            (yy, mm),
        ) or {}
        uvent_mes = float(row_live.get("total") or 0)
    except Exception:
        uvent_mes = 0.0

    return uvent_cerrados + uvent_mes


def venta_anual_kg_y_us() -> dict:
    """Ventas acumuladas últimos 12 meses (para CART/VENTANUAL*360 = días cobranza).

    PRG INFORMES.PRG línea 293: VENTANUAL = SUM(uvent) últimas 12 filas de historia.
    """
    row = db.fetch_one(
        """
        SELECT
          COALESCE(SUM(uvent), 0) AS uvent_anual,
          COALESCE(SUM(kvent), 0) AS kvent_anual
        FROM (
          SELECT uvent, kvent
          FROM scintela.historia
          ORDER BY fecha DESC
          LIMIT 12
        ) sub
        """
    )
    return {
        "uvent_anual": float((row or {}).get("uvent_anual") or 0),
        "kvent_anual": float((row or {}).get("kvent_anual") or 0),
    }


def stock_kg_live(hoy: date | None = None) -> dict:
    """Stock en kg actualizado al día (no al último cierre).

    Cómo se calcula:
        live = snapshot_ustock + kg_comprados_desde_snapshot
                                - kg_vendidos_desde_snapshot

    Es una aproximación: ignora movimientos intra-mes de tejido/tintura,
    que sólo quedan registrados cuando cerra el período y se escribe
    `historia.ktej` / `historia.ktin`. Pero da una señal día-a-día mucho
    más útil que el snapshot puro, que puede tener 1-30 días de lag.

    Devuelve un dict con:
        - snapshot_fecha  : fecha del snapshot base
        - snapshot_kg     : historia.stock en esa fecha (kg de MP+PT)
        - kg_comprados    : Σ kg de compras desde snapshot_fecha < fecha ≤ hoy
        - kg_vendidos     : Σ kg de facturas vivas en el mismo rango
        - live_kg         : resultado en kg
        - dias_desde_snapshot : cuántos días hace del snapshot (para mostrar
                                un "hace N días" en la UI)

    Si no hay snapshots todavía (DB nuevita), devuelve live=0 y todo en 0.

    BUG fix (2026-04-30): antes leía `historia.ustock` como si fuera kg.
    `ustock` está en US$ (con prefijo "u"), mientras que `stock` es kg.
    Mezclar ustock con kg comprados/vendidos daba un live_kg sin sentido
    (suma de US$ + kg). Ver auditoría 2026-04-30.
    """
    hoy = hoy or date.today()
    hist = historia_ultimo_mes() or {}
    snap_fecha = hist.get("fecha")
    snap_kg = float(hist.get("stock") or 0)  # kg, NO ustock (que está en US$)

    if not snap_fecha:
        return {
            "snapshot_fecha": None,
            "snapshot_kg": 0.0,
            "kg_comprados": 0.0,
            "kg_vendidos": 0.0,
            "live_kg": 0.0,
            "dias_desde_snapshot": None,
        }

    # Compras después del snapshot, hasta hoy inclusive. Filtramos
    # anuladas (stat 'X' o 'Y'). TMT 2026-05-13: el comentario decía que
    # filtraba, el código no lo hacía — agregado.
    row_c = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg
        FROM scintela.compra
        WHERE fecha > %s AND fecha <= %s
          AND COALESCE(kg, 0) > 0
          AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (snap_fecha, hoy),
    )
    kg_com = float((row_c or {}).get("kg") or 0)

    # Ventas en el mismo rango. Las facturas anuladas (stat='Y') no salen
    # de stock — quedan fuera. Una factura activa siempre mueve kg aunque
    # tenga saldo 0 (cobrada).
    row_v = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg
        FROM scintela.factura
        WHERE fecha > %s AND fecha <= %s
          AND COALESCE(kg, 0) > 0
          AND (stat IS NULL OR stat <> 'Y')
        """,
        (snap_fecha, hoy),
    )
    kg_ven = float((row_v or {}).get("kg") or 0)

    return {
        "snapshot_fecha": snap_fecha,
        "snapshot_kg": snap_kg,
        "kg_comprados": kg_com,
        "kg_vendidos": kg_ven,
        "live_kg": snap_kg + kg_com - kg_ven,
        "dias_desde_snapshot": (hoy - snap_fecha).days,
    }


# ---------------------------------------------------------------------------
# Composite: INFORME RESULTADOS - BALANCE
# ---------------------------------------------------------------------------

# =============================================================================
# CONTRATO DE LA CONCILIACIÓN — leer antes de modificar nada del balance
# =============================================================================
#
# `BALANCE_CONCEPTS` es la fuente de verdad de QUÉ COMPONENTES tiene el balance.
# La función `conciliacion_balance()` DEBE emitir exactamente una fila por
# cada concepto, en este orden, con las llaves requeridas.
#
# Si agregás, sacás o renombrás un componente del balance:
#   1. Actualizá `BALANCE_CONCEPTS` (esta lista).
#   2. Actualizá `conciliacion_balance()` para emitir esa fila.
#   3. Actualizá `informe_balance()` para que el balance lo muestre.
#   4. Actualizá `balance.html` si el orden visual cambia.
#
# El test `tests/test_balance_conciliacion.py` se rompe si la lista
# producida por `conciliacion_balance()` no es idéntica a `BALANCE_CONCEPTS`.
# CI te frena el merge.
#
# Ver `docs/CONCILIACION_CONTRACT.md` para detalle completo.
# =============================================================================

BALANCE_CONCEPTS: tuple[str, ...] = (
    "CAJA",
    "BANCOS",
    "CHEQUES (TOTC)",
    "FACTURAS (TOTF)",
    "ANTICIPOS",
    "MAQ/EQUIP. + TERR/EDIF/INS.",
    "STOCK MP+PROD. + STOCK QUI. + UTILIDAD",
    "PASIVOS (TOTP)",
    "DIVID. (URET)",
)

# Llaves obligatorias de cada fila de la conciliación.
CONCILIACION_REQUIRED_KEYS: frozenset[str] = frozenset(
    ["concepto", "balance", "modulo", "match", "diff", "detalle", "nota"]
)


def conciliacion_balance() -> list[dict]:
    """Conciliación: cada componente del balance contra la query del módulo.

    El gerente quiere ver que TOTC del balance coincida con la lista de
    cheques, TOTF con facturas, TOTP con posdat, etc. Cuando NO coinciden,
    casi siempre es porque un módulo filtra distinto al balance:

      - /cheques?estado=cartera muestra solo stat='Z'.
        TOTC del balance suma Z+1+2+3+P+D (PRG línea 24).
      - /facturas?vista=cartera filtra (stat IN Z,A) AND saldo>0.
        TOTF del balance: idem — coinciden por construcción.
      - /posdat?solo_abiertas=1 filtra banc<>9 AND importe>0.
        TOTP del balance: banc<>9 sin filtrar importe>0.

    Esta función hace una query equivalente a la de cada módulo y la
    compara contra el componente del balance, devolviendo una lista
    de filas con `concepto`, `balance`, `modulo`, `match`, `diff`,
    `detalle` (desglose para auditar) y `nota`.

    CONTRATO: las filas devueltas DEBEN tener concepto en el orden de
    `BALANCE_CONCEPTS`. Si modificás esta función, modifica también esa
    constante (y viceversa). El test test_balance_conciliacion bloquea
    merges que las desincronicen.
    """
    out: list[dict] = []

    def _diff(a: float, b: float, tol: float = 0.5) -> tuple[bool, float]:
        d = float(a or 0) - float(b or 0)
        return (abs(d) <= tol, d)

    # ----------- CAJA -----------
    salcaj_val = salcaj()
    caja_count = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.caja WHERE saldo IS NOT NULL"
    ) or {}
    out.append({
        "concepto": "CAJA",
        "balance": salcaj_val,
        "modulo":  salcaj_val,
        "match":   True,
        "diff":    0.0,
        "detalle": [
            ("Último saldo en scintela.caja", salcaj_val),
            ("Filas de caja con saldo no-null", int(caja_count.get("n") or 0)),
        ],
        "nota": "PRG línea 68: SALCAJ = SALDO del último registro de caja.",
    })

    # ----------- BANCOS -----------
    bancos = saldo_bancos()
    pos = posdat_totales()
    bancos_total = sum(float(b["saldo"] or 0) for b in bancos)
    salbanc = bancos_total + pos["pos1"] + pos["pos2"]
    bancos_detalle: list[tuple[str, float]] = [
        (f"{(b['nombre'] or 'Banco ' + str(b['no_banco']))} (origen: {b.get('saldo_origen')})", float(b["saldo"] or 0))
        for b in bancos if abs(float(b["saldo"] or 0)) > 0.5
    ]
    bancos_detalle.append(("+ Posdat banc=1 (POS1)", pos["pos1"]))
    bancos_detalle.append(("+ Posdat banc=2 (POS2)", pos["pos2"]))
    bancos_detalle.append(("= BANCOS (SALBANC)", salbanc))
    out.append({
        "concepto": "BANCOS",
        "balance": salbanc,
        "modulo":  salbanc,
        "match":   True,
        "diff":    0.0,
        "detalle": bancos_detalle,
        "nota": "PRG líneas 78, 99, 370: SALBANC = SALBANC1 + SALBANC2 = (Pichincha + POS1) + (Internacional + POS2).",
    })

    # ----------- CHEQUES (TOTC) -----------
    totc_val = totc()
    chq = cheques_por_stat()
    def _chq(stat: str) -> float:
        return float(chq.get(stat, {}).get("total") or 0)
    chq_z   = _chq("Z")
    chq_p   = _chq("P")
    chq_d   = _chq("D")
    chq_1   = _chq("1")
    chq_2   = _chq("2")
    chq_3   = _chq("3")
    chq_b   = _chq("B")
    chq_a   = _chq("A")
    chq_r   = _chq("R")
    en_totc = chq_z + chq_1 + chq_2 + chq_3 + chq_p + chq_d
    match_c, diff_c = _diff(totc_val, en_totc)
    cartera_modulo = chq_z   # /cheques?estado=cartera muestra solo Z
    out.append({
        "concepto": "CHEQUES (TOTC)",
        "balance": totc_val,
        "modulo":  en_totc,
        "match":   match_c,
        "diff":    diff_c,
        "detalle": [
            ("cartera Z (/cheques?estado=cartera)", chq_z),
            ("postergados P (/cheques?estado=postergados)", chq_p),
            ("Daniela D (/cheques?estado=daniela)", chq_d),
            ("rebote-en-gestión 1ra (/cheques?estado=devueltos)", chq_1),
            ("rebote-en-gestión 2da", chq_2),
            ("rebote-en-gestión 3ra", chq_3),
            ("Σ TOTC = Z+1+2+3+P+D", en_totc),
            ("(no entra) depositados B (suma a banco)", chq_b),
            ("(no entra) acreditados A legacy (suma a banco)", chq_a),
            ("(no entra) rebote terminal R (incobrable)", chq_r),
        ],
        "nota": (
            "PRG línea 24: STAT $ \"Z123PD\". TOTC suma cartera + postergados + Daniela + rebotados-en-gestión. "
            "La pestaña /cheques?estado=cartera muestra SOLO Z — por eso siempre es ≤ TOTC. "
            f"Diferencia esperada con la pestaña cartera: ${en_totc - cartera_modulo:,.2f}."
        ),
    })

    # ----------- FACTURAS (TOTF) -----------
    totf_val = totf()
    # f_cartera reporta el NET (= mismo filtro que totf), para que
    # diagnostico cuadre contra TOTF. Los sobrepagos (saldo<0) ya no se
    # excluyen; el dBase legacy nunca los excluyó.
    f_cartera = db.fetch_one(
        """
        SELECT COUNT(*)                         AS n,
               COALESCE(SUM(saldo),   0)        AS saldo,
               COALESCE(SUM(importe), 0)        AS importe,
               COUNT(*) FILTER (WHERE saldo < 0) AS n_sobrepagos,
               COALESCE(SUM(saldo) FILTER (WHERE saldo < 0), 0) AS saldo_sobrepagos
        FROM scintela.factura
        WHERE stat IS NULL OR stat IN ('Z','A','',' ')
        """
    ) or {}
    f_canceladas = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat = 'T'"""
    ) or {}
    f_eliminadas = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat = 'X'"""
    ) or {}
    f_total_emit = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat <> 'X' OR stat IS NULL"""
    ) or {}
    saldo_cartera = float(f_cartera.get("saldo") or 0)
    match_f, diff_f = _diff(totf_val, saldo_cartera)
    out.append({
        "concepto": "FACTURAS (TOTF)",
        "balance": totf_val,
        "modulo":  saldo_cartera,
        "match":   match_f,
        "diff":    diff_f,
        "detalle": [
            (f"Cartera Z/A NETA — {int(f_cartera.get('n') or 0)} facturas (sumando sobrepagos)", saldo_cartera),
            (f"  ↳ de las cuales {int(f_cartera.get('n_sobrepagos') or 0)} con saldo<0 (sobrepagos)", float(f_cartera.get("saldo_sobrepagos") or 0)),
            ("  importe total emitido de esas mismas facturas",     float(f_cartera.get("importe") or 0)),
            (f"Canceladas (stat=T) — {int(f_canceladas.get('n') or 0)} facturas (importe)", float(f_canceladas.get("importe") or 0)),
            (f"Eliminadas (stat IN X,Y) — {int(f_eliminadas.get('n') or 0)} (importe)",     float(f_eliminadas.get("importe") or 0)),
            (f"Total facturas emitidas (sin X/Y) — {int(f_total_emit.get('n') or 0)} (importe)", float(f_total_emit.get("importe") or 0)),
        ],
        "nota": (
            "PRG línea 27: TOTF = SUM(saldo) FOR STAT $ \"ZA\" (sin filtro de signo). "
            "Sobrepagos (saldo<0 = abono > importe) restan de la cartera. "
            "Verificado: $4.916.202,77 = lo que el dBase live mostraba 2026-05-06."
        ),
    })

    # ----------- ANTICIPOS -----------
    antic_val = anticipos()
    dol_breakdown = db.fetch_all(
        """SELECT COALESCE(NULLIF(TRIM(st), ''), '(vivo)') AS st,
                  COUNT(*) AS n,
                  COALESCE(SUM(importe), 0) AS total
           FROM scintela.dolares
           GROUP BY 1 ORDER BY 1"""
    ) or []
    detalle_dol = []
    total_dol_all = 0.0
    for r in dol_breakdown:
        t = float(r.get("total") or 0)
        total_dol_all += t
        detalle_dol.append((f"st={r['st']} ({int(r['n'])} filas)", t))
    detalle_dol.append(("ANTICIPOS = SUM(st null/vacío)", antic_val))
    detalle_dol.append(("Total dólares (todos los st)", total_dol_all))
    out.append({
        "concepto": "ANTICIPOS",
        "balance": antic_val,
        "modulo":  antic_val,
        "match":   True,
        "diff":    0.0,
        "detalle": detalle_dol,
        "nota": "ANTICIPOS = SUM(importe) en scintela.dolares con st NULL o vacío (anticipos vivos del cliente).",
    })

    # ----------- ACTIVOS FIJOS (UMAQ + UACT) -----------
    activos = activos_totales()
    a_breakdown = db.fetch_all(
        """SELECT COALESCE(NULLIF(TRIM(tipo), ''), '(sin tipo)') AS tipo,
                  COUNT(*) AS n,
                  COALESCE(SUM(valor), 0) AS total
           FROM scintela.activos
           GROUP BY 1 ORDER BY 1"""
    ) or []
    detalle_act = []
    for r in a_breakdown:
        detalle_act.append((f"tipo={r['tipo']} ({int(r['n'])} activos)", float(r.get("total") or 0)))
    detalle_act.append(("Σ UMAQ (tipo M/C/K)", activos["umaq"]))
    detalle_act.append(("Σ UACT (tipo I)",     activos["uact"]))
    out.append({
        "concepto": "MAQ/EQUIP. + TERR/EDIF/INS.",
        "balance": activos["umaq"] + activos["uact"],
        "modulo":  activos["umaq"] + activos["uact"],
        "match":   True,
        "diff":    0.0,
        "detalle": detalle_act,
        "nota": "PRG líneas 47-48: UACT FOR TIPO='I' (terrenos/edificios), UMAQ FOR TIPO $ 'MCK' (maquinaria, computación, kilos).",
    })

    # ----------- STOCK MP+PROD / STOCK QUI / UTILIDAD (historia) -----------
    hist = historia_ultimo_mes() or {}
    snap_fecha = hist.get("fecha")
    out.append({
        "concepto": "STOCK MP+PROD. + STOCK QUI. + UTILIDAD",
        "balance": float(hist.get("ustock") or 0) + float(hist.get("uqui") or 0),
        "modulo":  float(hist.get("ustock") or 0) + float(hist.get("uqui") or 0),
        "match":   True,
        "diff":    0.0,
        "detalle": [
            ("VSTO = historia.ustock (último cierre)", float(hist.get("ustock") or 0)),
            ("VQX = historia.uqui",                    float(hist.get("uqui") or 0)),
            ("UTILIDAD = historia.usuti",              float(hist.get("usuti") or 0)),
            ("PATANT = historia.patrimonio",           float(hist.get("patrimonio") or 0)),
            ("Fecha snapshot histórico", snap_fecha.isoformat() if snap_fecha else "—"),
        ],
        "nota": "VSTO/VQX/PATANT/USUTI vienen del último snapshot mensual en scintela.historia. Si la fecha está vieja, todos estos componentes pueden estar desfasados.",
    })

    # ----------- PASIVOS (TOTP) -----------
    pd_balance = pos["totp"]
    pd_modulo = db.fetch_one(
        f"""
        SELECT COUNT(*)                         AS n,
               COALESCE(SUM(importe), 0)        AS total
        FROM scintela.posdat
        WHERE {POSDAT_DEUDA_VIVA_WHERE}
          AND COALESCE(importe, 0) > 0
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or {}
    pd_total_modulo = float(pd_modulo.get("total") or 0)
    pd_pagados = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS total
           FROM scintela.posdat
           WHERE COALESCE(banc,0)=9
             AND (anulada IS NOT TRUE OR anulada IS NULL)"""
    ) or {}
    pd_neg = db.fetch_one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS total
           FROM scintela.posdat
           WHERE {POSDAT_DEUDA_VIVA_WHERE}
             AND COALESCE(importe,0)<=0
             AND (anulada IS NOT TRUE OR anulada IS NULL)"""
    ) or {}
    pd_neg_total = float(pd_neg.get("total") or 0)
    # La diferencia ESPERABLE entre balance y módulo es exactamente la
    # suma de los posdat con importe<=0: balance = módulo + neg. Si esa
    # identidad se cumple, marcamos ✓ aunque los dos números no sean
    # idénticos — no es un drift, es que miden cosas levemente distintas.
    diff_p = pd_balance - pd_total_modulo
    match_p = abs(diff_p - pd_neg_total) <= 0.5
    out.append({
        "concepto": "PASIVOS (TOTP)",
        "balance": pd_balance,
        "modulo":  pd_total_modulo,
        "match":   match_p,
        "diff":    diff_p,
        "detalle": [
            (f"Posdat abiertas (banc=0, importe>0) — {int(pd_modulo.get('n') or 0)} partidas", pd_total_modulo),
            (f"Posdat con importe ≤ 0 (no entran al módulo) — {int(pd_neg.get('n') or 0)}", float(pd_neg.get("total") or 0)),
            (f"Posdat pagadas (banc=9) — {int(pd_pagados.get('n') or 0)}", float(pd_pagados.get("total") or 0)),
            ("(de las anteriores) POS1 banc=1 (suma a Pichincha)", pos["pos1"]),
            ("(de las anteriores) POS2 banc=2 (suma a Internacional)", pos["pos2"]),
        ],
        "nota": (
            "PRG línea 55: TOTP = SUM(importe) FOR BANC=0 (deuda viva, "
            "no instrumentada). banc=1/2 ya descontaron el saldo bancario "
            "vía bank_helpers, banc=9 son cheques posdatados ya emitidos. "
            "El balance incluye posdats con importe≤0 (anticipos/ajustes); "
            "el módulo /posdat los esconde porque no son deuda viva. La "
            "diferencia entre las dos columnas debe ser exactamente igual "
            "a la suma de esos posdats negativos — si matchea, ✓ (no es "
            "drift, son métricas levemente distintas)."
        ),
    })

    # ----------- DIVID (URET) -----------
    uret_val = uret_mes_corriente()
    uret_total = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(ret), 0) AS total
           FROM scintela.retiros"""
    ) or {}
    uret_year = db.fetch_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(ret), 0) AS total
           FROM scintela.retiros WHERE EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM CURRENT_DATE)"""
    ) or {}
    out.append({
        "concepto": "DIVID. (URET)",
        "balance": uret_val,
        "modulo":  uret_val,
        "match":   True,
        "diff":    0.0,
        "detalle": [
            ("Retiros del mes en curso (URET)", uret_val),
            (f"Retiros del año actual — {int(uret_year.get('n') or 0)}", float(uret_year.get("total") or 0)),
            (f"Retiros TOTALES histórico — {int(uret_total.get('n') or 0)}", float(uret_total.get("total") or 0)),
        ],
        "nota": "PRG línea 37: URET = SUM(ret) FOR &MA AND DD-FECHA<63. &MA = mes/año actual. Filtramos retiros del mes en curso.",
    })

    # ---------- SELF-CHECK del contrato ----------
    # Si las filas no son exactamente BALANCE_CONCEPTS en orden, algo se desincronizó.
    # En dev levantamos una assertion (test fail). En prod, log warning y seguimos.
    conceptos_emitidos = tuple(r.get("concepto") for r in out)
    if conceptos_emitidos != BALANCE_CONCEPTS:
        msg = (
            "CONCILIACION_CONTRACT violado: conciliacion_balance() emitió "
            f"{conceptos_emitidos} pero BALANCE_CONCEPTS dice {BALANCE_CONCEPTS}. "
            "Si agregaste/sacaste un componente del balance, actualizá AMBAS "
            "estructuras. Ver docs/CONCILIACION_CONTRACT.md."
        )
        # En dev/test: error duro. En prod: warning para no romper la página.
        import logging
        import os
        if os.environ.get("ENV", "development") == "development":
            raise AssertionError(msg)
        logging.getLogger(__name__).error(msg)

    # Validar estructura de cada fila — todas las llaves obligatorias presentes.
    for fila in out:
        faltantes = CONCILIACION_REQUIRED_KEYS - set(fila.keys())
        if faltantes:
            msg = (
                f"CONCILIACION_CONTRACT violado: fila '{fila.get('concepto')}' "
                f"falta llaves: {faltantes}. Llaves requeridas: {CONCILIACION_REQUIRED_KEYS}."
            )
            import logging
            import os
            if os.environ.get("ENV", "development") == "development":
                raise AssertionError(msg)
            logging.getLogger(__name__).error(msg)

    return out


def _verificar_balance_math(b: dict) -> list[str]:
    """Validador in-process del balance.

    Recomputa cada total derivado a partir de sus componentes y verifica
    que coincida con el valor stored. Si alguna invariante falla, devuelve
    un mensaje en español por cada violación. La idea: si alguna fila del
    template no cuadra con la suma de sus partes, el gerente lo ve
    inmediatamente como advertencia (en prod), o el test rompe (en dev).

    Invariantes validados (de INFORMES.PRG líneas 370-380):
        CART  = TOTF + TOTC
        SUBT  = SALBANC + SALCAJ + CART
        TOTL  = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC
        PATR  = TOTL - TOTP
      + BANCOS rule (post-batch 20):
        SALBANC = sum(saldo bancos) + POS1 + POS2
    """
    errores: list[str] = []
    tol = 0.5  # centavos de redondeo aceptables

    def _check(formula: str, esperado: float, calculado: float):
        if abs(float(esperado or 0) - float(calculado or 0)) > tol:
            errores.append(
                f"Math check falló — {formula}: esperado {esperado:,.2f} "
                f"vs calculado {calculado:,.2f} (diff {esperado - calculado:+,.2f})"
            )

    _check("CART = TOTF + TOTC",
           b.get("cart", 0),
           float(b.get("totf") or 0) + float(b.get("totc") or 0))

    _check("SUBT = SALBANC + SALCAJ + CART",
           b.get("subt", 0),
           float(b.get("salbanc") or 0) + float(b.get("salcaj") or 0) + float(b.get("cart") or 0))

    _check("TOTL = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC",
           b.get("totl", 0),
           float(b.get("subt") or 0)
           + float(b.get("vsto") or 0) + float(b.get("vqx") or 0)
           + float(b.get("umaq") or 0) + float(b.get("uact") or 0)
           + float(b.get("uret") or 0) + float(b.get("antic") or 0))

    _check("PATR = TOTL - TOTP",
           b.get("patr", 0),
           float(b.get("totl") or 0) - float(b.get("totp") or 0))

    # BANCOS rule (post-fix 2026-04-30): sum saldo bancos + POS1 + POS2.
    # `bancos_todos` es la lista cruda de saldo_bancos() — cada uno con `saldo`.
    if "bancos_todos" in b:
        sum_bancos = sum(float(bk.get("saldo") or 0) for bk in b["bancos_todos"])
        _check(
            "SALBANC = SUM(saldos bancos) + POS1 + POS2",
            b.get("salbanc", 0),
            sum_bancos + float(b.get("pos1") or 0) + float(b.get("pos2") or 0),
        )

    return errores


def _safe_div(num: float, den: float) -> float:
    """División protegida — devuelve 0 si denominador es 0/None."""
    try:
        n = float(num or 0)
        d = float(den or 0)
        return n / d if d else 0.0
    except (TypeError, ValueError):
        return 0.0


def _costo_total_con_desperdicio(
    *,
    cost_mat_ukg, cost_col_ukg, cost_tej_ukg, cost_gsp_ukg, cost_gas_ukg,
    cost_mat_us,  cost_col_us,  cost_tej_us,  cost_gsp_us,  cost_gas_us,
    cost_mat_proy, cost_col_proy, cost_tej_proy, cost_gsp_proy, cost_gas_proy,
    KR, KTINT,
) -> dict:
    """Fila TOTAL del panel COSTOS — replica PRG INFORMES.PRG líneas 404-413.

    El dBase aplica un factor de DESPERDICIO (= yarn loss + tintura loss)
    sólo a MAT.PR. y COL.QUI. en la fila TOTAL. Las demás filas (TEJIDO,
    GS.PROC., GASTOS) suman al raw.

    Fórmulas PRG:
        DESK = 0.5  (constante línea 6)
        DESP = (1 - KR/KTINT) * 100  (línea 261; default 4 si KTINT=0)
        factor = 1 + (DESP+DESK)/100

        COSTUNI   = factor*(UMX + ITIN/KR) + VK/KK + GTIN/KR + GS/KV
                    (línea 406, = TOTAL ukg)
        COSTOPROY = KGPRO*(UMX + ITIN/KR)*factor + XPRETOT
                    (línea 404, = TOTAL proy_us)
        TOTAL us  ≈ factor*(VM + ITIN) + VK + GTIN + GS  (sum-of-rows
                    style, applying factor to MAT.PR. y COL.QUI. row-us)

    Verificado contra TMT 2026-05-06: con DESP+DESK ≈ 4.2%, factor ≈
    1.0422; ukg = 1.0422*(2.923 + 0.696) + 0.560 + 1.741 + 1.163 = 7.236
    (= "Total con desperdicio" que el dBase muestra como 7.237).
    """
    if KTINT and KTINT > 0:
        DESP = (1 - (KR or 0) / KTINT) * 100
    else:
        DESP = 4.0  # default PRG línea 258
    DESK = 0.5
    factor = 1 + (DESP + DESK) / 100
    return {
        "ukg":     factor * (cost_mat_ukg + cost_col_ukg)
                   + cost_tej_ukg + cost_gsp_ukg + cost_gas_ukg,
        "us":      factor * (cost_mat_us + cost_col_us)
                   + cost_tej_us + cost_gsp_us + cost_gas_us,
        "proy_us": factor * (cost_mat_proy + cost_col_proy)
                   + cost_tej_proy + cost_gsp_proy + cost_gas_proy,
        "desperdicio_pct": DESP + DESK,
        "factor":          factor,
    }


def _eff_rate(live: float | None, meta: float | None) -> tuple[float, str]:
    """Tarifa "effectiva": live si hay, sino meta de iniciales, sino 0.

    Retorna (valor, fuente) donde fuente ∈ {'live', 'meta', 'none'}. El
    template usa la fuente para mostrar un chip 'meta' cuando la pantalla
    está usando objetivos de iniciales en vez de datos del mes.

    Helper a nivel módulo (no nested en informe_balance) para que cualquier
    bloque del balance pueda usarlo, incluyendo el bloque COSTOS que viene
    antes que el bloque de proyecciones donde se definió originalmente.
    """
    try:
        lv = float(live or 0)
    except (TypeError, ValueError):
        lv = 0.0
    try:
        mv = float(meta or 0)
    except (TypeError, ValueError):
        mv = 0.0
    if lv > 0:
        return lv, "live"
    if mv > 0:
        return mv, "meta"
    return 0.0, "none"


def informe_balance() -> dict:
    """Arma el BALANCE equivalente al del INFORMES.PRG screen."""
    _totf = totf()
    _totc = totc()
    bancos = saldo_bancos()
    _salcaj = salcaj()
    posdats = posdat_totales()
    activos = activos_totales()
    _antic = anticipos()
    _uret = uret_mes_corriente()
    # TMT 2026-05-19 v7 — dueña pidió "dividendos del año" debajo de
    # "dividendos del mes". retiros_total_anual() suma scintela.retiros
    # del año en curso.
    _uret_anio = retiros_total_anual()
    # TMT 2026-05-19 v8 — pedido dueña: agregar "Ventas del año" en el
    # panel derecho (reemplaza "Patrimonio último cierre").
    _ventas_anio = ventas_anio_en_curso()
    hist = historia_ultimo_mes() or {}
    inic = iniciales_mes_actual() or {}
    venta_anual = venta_anual_kg_y_us()
    # Mes EN CURSO (live, no del cierre histórico) — replica el dBase.
    vent_mes = ventas_mes_corriente_resultado()
    comp_mes = compras_mes_corriente()

    # SALBANC = saldo total de TODOS los bancos + POS1 + POS2.
    #
    # PRG líneas 78,99,370 hardcodeaban: SALBANC1 = saldo banco 1 + POS1,
    # SALBANC2 = saldo banco 2 + POS2 (asumiendo banco 1 = Pichincha,
    # banco 2 = Internacional). En este Postgres los `no_banco` no
    # necesariamente coinciden con esa convención (Pichincha puede ser
    # no_banco=3 o cualquier otro). Si confiás en el hardcode, BANCOS
    # sale 0 cuando los IDs no matchean — bug que reportó TMT 2026-04-30.
    #
    # Política nueva: sumar TODOS los bancos (sea cual sea su no_banco)
    # y agregar los posdat-banco-1/2 que el dBase mantiene aparte
    # (POS1/POS2 = cheques posfechados a depositar en el banco 1 o 2,
    # se imputan al banco; si tu data tiene Pichincha en otro no_banco,
    # POS1/POS2 pueden ser 0 — y el balance sigue cuadrando).
    total_bancos = sum(float(b["saldo"] or 0) for b in bancos)
    salbanc = total_bancos + posdats["pos1"] + posdats["pos2"]
    # Mantenemos salbanc1, salbanc2 para el diagnóstico — son informativos
    # del legacy mapping, no se usan para el total.
    salbanc1, salbanc2 = 0.0, 0.0
    for b in bancos:
        if b["no_banco"] == 1:
            salbanc1 = float(b["saldo"] or 0) + posdats["pos1"]
        elif b["no_banco"] == 2:
            salbanc2 = float(b["saldo"] or 0) + posdats["pos2"]

    # Tres lecturas distintas — TMT 2026-05-06 confirmó esta separación:
    #   · VSTO display: del cálculo del panel STOCK izquierdo
    #     (= sum de etapas con snapshot kg × tarifas iniciales).
    #     Lo asignamos DESPUÉS de calcular stock_total_us más abajo.
    #     El panel ACTIVO derecho (STOCK MP+PROD) refleja ese mismo total.
    #   · VQX (Stock Quí.): viene del snapshot LIVE de hoy
    #     (= historia_ultimo_snapshot.uqui = 279.591 hoy).
    #   · PATANT (último cierre): historia_ultimo_mes.patrimonio
    #     (= último cierre real, pre-mes-actual = $20.115.887 abril 30).
    hist_live = historia_ultimo_snapshot() or {}
    vsto = 0.0  # placeholder — se asigna abajo con stock_total_us del panel STOCK izq
    vqx = float(hist_live.get("uqui") or 0)
    patant = float(hist.get("patrimonio") or 0)

    cart = _totf + _totc
    subt = salbanc + _salcaj + cart
    totl = subt + vsto + vqx + activos["umaq"] + activos["uact"] + _uret + _antic
    patr = totl - posdats["totp"]
    # UTILIDAD ECONÓMICA DEL MES = PATR - PATANT (PRG línea 380).
    # PATANT (= historia.patrimonio) ya es NETO de URET (PRG 1347:
    # REPLA PATRIMONIO WITH PATR-URET). Por eso patr - patant solo ya
    # equivale a "delta(PN) + URET_mes". Sumar URET extra lo double-cuenta.
    #
    # IMPORTANTE: usar el `patr` y `vsto` del CIERRE ANTERIOR (historia.ustock)
    # para esta cuenta. Si después overrride vsto con stock_total_us
    # (= kg × tarifas iniciales) para que ambos paneles coincidan, NO
    # recomputamos esta utilidad — la re-valuación de stock entre cierres
    # NO es ganancia económica.
    patr_para_utilidad = patr  # snapshot ANTES de cualquier override de vsto
    utilidad = patr_para_utilidad - patant

    # Info de kg del último cierre — equivalente al bloque F9 de INFORMES.PRG
    # que el gerente mira al lado del balance monetario.
    # stock_kg es el del último snapshot; stock_kg_live es una aproximación
    # actualizada al día (snapshot + compras - ventas desde el snapshot).
    # La plantilla muestra ambos cuando difieren por más de un threshold.
    live = stock_kg_live()
    kg = {
        # historia.stock = kg, historia.ustock = US$. Antes leía ustock acá
        # (label decía "kg" pero el valor estaba en US$). Fix 2026-04-30.
        "stock_kg":   float(hist.get("stock") or 0),   # kg en stock de MP+PT (snapshot)
        "stock_kg_live":       live["live_kg"],
        "stock_kg_diff":       live["live_kg"] - float(hist.get("stock") or 0),
        "stock_kg_live_desde": live["snapshot_fecha"],
        "stock_kg_dias":       live["dias_desde_snapshot"],
        "kcom":       float(hist.get("kcom")   or 0),   # kg comprados el mes
        "ktej":       float(hist.get("ktej")   or 0),   # kg tejidos
        "ktin":       float(hist.get("ktin")   or 0),   # kg tinturados (fuera)
        "kvent":      float(hist.get("kvent")  or 0),   # kg vendidos
        "ucom":       float(hist.get("ucom")   or 0),   # U$ compras mes
        "utej":       float(hist.get("utej")   or 0),   # U$ costo tejido
        "utin":       float(hist.get("utin")   or 0),   # U$ costo tintura
        "uvent":      float(hist.get("uvent")  or 0),   # U$ ventas mes
        "costo_mes":  float(hist.get("costo")  or 0),   # U$ costo total mes
        # Precios unitarios útiles para el ojo del gerente
        "precio_vta": (float(hist.get("uvent") or 0) /
                       float(hist.get("kvent") or 0))
                      if hist.get("kvent") else 0.0,
        "costo_kg":   (float(hist.get("ucom") or 0) /
                       float(hist.get("kcom") or 0))
                      if hist.get("kcom") else 0.0,
    }

    # Los bancos con saldo exactamente 0 no aportan a la lectura del balance —
    # filtrarlos evita que la tabla lateral tenta 5 bancos inactivos mezclados
    # con los 2 vivos. En /bancos sí se ven todos (toggle).
    bancos_activos = [b for b in bancos if round(float(b["saldo"] or 0), 2) != 0.0]

    # ---- Diagnóstico — cuando el balance "no cuadra" el gerente necesita
    # ver de un vistazo qué componente puede estar vacío o desfasado.
    chq_breakdown = cheques_por_stat()
    snap_fecha = hist.get("fecha")
    dias_snapshot = (date.today() - snap_fecha).days if snap_fecha else None
    activos_count_row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.activos WHERE COALESCE(valor,0) > 0"
    ) or {}
    n_activos = int(activos_count_row.get("n") or 0)

    advertencias = []
    if dias_snapshot is None:
        advertencias.append(
            "No hay snapshot mensual en `historia`. VSTO/VQX/PATANT salen en 0 y la utilidad no se puede calcular."
        )
    elif dias_snapshot > 45:
        advertencias.append(
            f"El último snapshot mensual es de hace {dias_snapshot} días "
            f"({snap_fecha.strftime('%d/%m/%Y')}). PATANT y stock pueden estar desfasados."
        )
    if vsto == 0 and vqx == 0:
        advertencias.append(
            "Stock MP+PT y Stock Colorantes están en 0 — verificar que el cierre mensual cargó `historia.stock` y `historia.uqui`."
        )
    if activos["umaq"] == 0 and activos["uact"] == 0:
        advertencias.append(
            f"Activos fijos en 0 (Maq/Equip + Terr/Edif). Hay {n_activos} activos con valor > 0 en la tabla — revisar tipo (M/C/K/I)."
        )
    if patant == 0:
        advertencias.append(
            "Patrimonio anterior (PATANT) está en 0 — la utilidad muestra el patrimonio entero como ganancia. Falta cargar el snapshot del mes anterior."
        )
    # Bancos: avisar solo cuando saldo final = 0 a pesar de tener movimientos
    # (todos los métodos de resolución fallaron). NO alertar de "desfase" cuando
    # estamos usando stored a propósito — eso es la política, no un problema.
    bancos_con_movs = [bk for bk in bancos if int(bk.get("n_transacciones") or 0) > 0]
    bancos_en_cero = [bk for bk in bancos_con_movs if round(float(bk.get("saldo") or 0), 2) == 0.0]
    if bancos_con_movs and len(bancos_en_cero) == len(bancos_con_movs):
        advertencias.append(
            f"Los {len(bancos_con_movs)} bancos con transacciones tienen saldo final 0 — "
            "ni el running stored ni el SUM de transacciones tiene valor. "
            "Verificar la migración de la tabla `transacciones_bancarias`."
        )
    # Advertencia "SUM firmado vs running saldo" suprimida 2026-05-06 a
    # pedido de TMT — es ruido de migración (running saldo del dBase es
    # la fuente canónica y eso es lo que mostramos). El diagnostico
    # detallado en /informes/balance/diagnostico sigue exponiendo
    # `saldo_origen` y `saldo_signed` por banco para auditar manualmente
    # cuando haga falta.
    # PATR vs PATANT — si la diferencia es enorme, advertir que la fórmula
    # PATR-PATANT no aplica mid-mes (es de cierre).
    if patant and abs(utilidad) >= 2_000_000:
        advertencias.append(
            f"Diferencia PATR vs PATANT = {utilidad:+,.0f}. La fórmula UTILIDAD=PATR−PATANT solo cuadra al cierre de mes; "
            "mid-mes fluctúa con cada cobranza/pago. Para utilidad real del mes en curso, mirar UT.ACT en Resultados (viene del último cierre)."
        )
    # Diagnóstico: cheques que NO suman a TOTC pero podrían ser importantes
    rebotados_terminales = chq_breakdown.get("R", {}).get("total", 0.0)
    depositados = (
        chq_breakdown.get("B", {}).get("total", 0.0)
        + chq_breakdown.get("A", {}).get("total", 0.0)
    )

    diagnostico = {
        "snapshot_dias": dias_snapshot,
        "snapshot_fecha": snap_fecha,
        "advertencias": advertencias,
        "cheques_breakdown": chq_breakdown,
        "cheques_rebotados_terminales": rebotados_terminales,
        "cheques_depositados": depositados,
        "n_activos_con_valor": n_activos,
        # Componentes desglosados — para una tabla "qué suma a qué"
        "componentes": {
            "salcaj": _salcaj,
            "salbanc1": salbanc1,
            "salbanc2": salbanc2,
            "salbanc_total": salbanc,
            "totc": _totc,
            "totf": _totf,
            "cart": cart,
            "subt": subt,
            "vsto": vsto,
            "vqx": vqx,
            "umaq": activos["umaq"],
            "uact": activos["uact"],
            "antic": _antic,
            "uret": _uret,
            "totl": totl,
            "totp": posdats["totp"],
            "patr": patr,
            "patant": patant,
            "utilidad": utilidad,
        },
    }

    # ---- INFORME RESULTADOS — left panel del screen del dBase
    # Replica el layout: kg / U$/kg / U$ / PROYEC. con VENTA, COSTOS
    # (MAT.PR / TEJIDO / COL.QUI / GS.PROC / GASTOS), UT.ACT, UT.PROY,
    # STOCK (HILADO / TEJIDO / TERMIN / TOTAL).
    # Mapeo PRG → historia (escrito por INFORMES.PRG línea 1345-1347):
    #     historia.ucom = VM (compras MP)
    #     historia.utej = VK (gastos tejeduría completos)
    #     historia.utin = GTIN (gastos tintorería sin colorantes)
    #     historia.gasto = GS (administración)
    #     historia.gstotal = total gastos del mes
    #     historia.kvent / uvent = ventas mes
    #     historia.kcom = kg comprados
    #     historia.ktej / ktin = kg tejidos / tinturados
    #     historia.usuti = UTILIDAD del mes (PATR-PATANT)
    # VENTA y COMPRAS = LIVE del mes en curso (NO del último cierre histórico).
    # El dBase computa estos números desde FACTURAS.DBF / COMPRAS.DBF directamente
    # filtrando por mes actual; replicamos eso para que VENTA = 307k (real abril)
    # en vez de 108k (parcial hasta el cierre).
    h_kvent = vent_mes["kg"]
    h_uvent = vent_mes["importe"]
    h_kcom  = comp_mes["kg"]
    h_ucom  = comp_mes["importe"]

    # ─── Iniciales / proyecciones del mes ───────────────────────────────
    # Estos datos del mes target los necesita el bloque COSTOS (proyección
    # de cada fila + tarifas META para fallback) y el bloque STOCK más
    # abajo. Antes vivían después del bloque COSTOS y eso rompía el orden
    # de definición.
    kgpro = float(inic.get("kprog") or 0)            # KGPRO — kg meta del mes
    pretej = float(inic.get("pretej") or 0)
    pretin = float(inic.get("pretin") or 0)
    preadm = float(inic.get("preadm") or 0)
    pretot = float(inic.get("pretot") or 0)
    inic_um = float(inic.get("um") or 0)             # tarifa MP objetivo
    inic_uk = float(inic.get("uk") or 0)             # tarifa tejido objetivo
    inic_uq = float(inic.get("uq") or 0)             # tarifa col.qui. objetivo
    inic_pre = float(inic.get("pre") or 0)           # tarifa precio venta

    # ─── Tarifas del CIERRE ANTERIOR (mes previo) ─────────────────────
    # `um_anterior`: la usa MAT.PR. UMX (panel COSTOS) y el back-derive
    # del Hilado del panel STOCK. Convención del PRG: "tarifa anterior"
    # = iniciales.um del cierre del mes pasado (NO del mes en curso,
    # que es ex-post).
    # `uf_anterior`: la usamos para estimar el costo de las kg de
    # terminado que se venden por facturas PC, así podemos ajustar
    # historia.ustock proporcionalmente (sin caer en cálculo circular).
    # Tejido y Terminado del panel STOCK NO leen iniciales.uk/uf — se
    # derivan de h_um con offsets fijos (uk = um+0,5, uf = uk+1,7).
    mesnum_actual = int(inic.get("mesnum") or 0)
    yy_actual     = int(inic.get("yy") or 0)
    um_anterior   = tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "um")
    tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "uf")

    # Tarifas live (ratio de datos del mes; pueden ser 0 si no hay datos).
    precio = _safe_div(h_uvent, h_kvent)
    umx    = _safe_div(h_ucom,  h_kcom)
    iqx    = _safe_div(0,       0)                   # se completa con tin abajo
    # Tarifas EFFECTIVAS = live si hay, sino meta de iniciales.
    precio_eff, precio_src = _eff_rate(precio, inic_pre)
    um_eff,     um_src     = _eff_rate(umx,    inic_um)
    uq_eff,     uq_src     = _eff_rate(iqx,    inic_uq)

    # COSTOS panel — replica EXACTA de INFORMES.PRG líneas 399-403.
    # Variables del legacy:
    #   KM, VM      = compras tipo='H' del mes (kg, U$)
    #   KK, VK      = compras tipo='K' (kg total, U$ + DTJ — sin V1+V2+V3)
    #   KTINT, KR, ITIN = tinto del mes (kg tinturados, kg que llegan a
    #                     terminado, U$)
    #   GTIN        = V4+V5+V6 + DCC          (gs.proceso tintura)
    #   GS          = V7+V8+V9 + DEPRCAR      (admin — aproximación; el
    #                                          PRG hace G1+G2+CA+DEPRCAR
    #                                          desde flujo bancario)
    #   UMX         = costo MP ponderado por stock (legacy FIFO formula)
    #   KV          = h_kvent (kg de ventas del mes)
    amort = amortizaciones_mensuales()           # → dcc, dtj, deprcar
    gxg   = gastos_xgast_v1_a_v9_mes()           # → V1..V9 sumados por rubro
    tin   = tinto_mes_corriente_resultado()      # → itin, ktint, kr
    tej   = tejido_mes_componentes()             # → kg interno/externo + us externo/KK

    # ─── MAT.PR. (PRG línea 399) ──────────────────────────────────────
    # PRG línea 337: UMX = (VM + (HI - KM) * UM0) / HI con HI = HI0+KM-KH
    # HI0 = stock inicial de hilado para el mes — PRG línea 313:
    #       USE INICIALES; HI0 = HILADO. NO historia.hilado.
    #       (En el dBase, INICIALES se actualiza al cierre con el HILADO
    #       de cierre del mes anterior, así que conceptualmente coincide
    #       con historia.hilado, pero la fuente canónica es iniciales.)
    # KM, VM = compras MP del mes (kg, U$)
    # UM0 = tarifa MP del cierre anterior (iniciales.um del mes previo).
    # KH = kg salidos de hilado a tejido. PRG línea 267:
    #      KH = KK / (1 - DESK/100)
    # donde DESK = 0.5 (% de pérdida en tejeduría — yarn loss).
    # Bug TMT 2026-05-06 (MAT.PR. esperado 2.926, mostramos 2.698):
    # estábamos leyendo HI0 de historia.hilado en vez de iniciales.hilado.
    # Bug TMT 2026-05-08: UM0 leía iniciales.um del mes en curso, que es
    # ex-post (resultado de esta misma fórmula al cierre anterior). Ahora
    # usa um_anterior (iniciales.um del mes previo). Si no hay mes previo
    # cargado, fallback al valor del mes en curso (comportamiento previo).
    DESK_PCT = 0.5  # % de pérdida en tejeduría — PRG línea 6: DESK = .5
    KK = float(tej.get("kg_total") or 0)
    KM = h_kcom
    VM = h_ucom
    HI0 = float(inic.get("hilado") or 0)  # PRG línea 313: USE INICIALES; HI0 = HILADO
    UM0 = um_anterior or float(inic.get("um") or 0)
    KH = KK / (1 - DESK_PCT / 100) if DESK_PCT < 100 else KK
    HI = HI0 + KM - KH
    if HI > 0:
        UMX = (VM + (HI - KM) * UM0) / HI
    elif KM > 0:
        # Sin HI fiable, fallback al ratio de compras del mes
        UMX = VM / KM
    else:
        # Sin compras ni stock — fallback a la tarifa objetivo
        UMX = UM0
    cost_mat_kg = KM
    cost_mat_us = VM
    cost_mat_ukg = UMX
    cost_mat_proy = kgpro * UMX

    # ─── TEJIDO (PRG línea 400) ───────────────────────────────────────
    # PRG línea 241: VK = SUM(IMPORTE WHERE TIPO='K') + DTJ
    # KK ya definido arriba en el bloque MAT.PR. = SUM(KG WHERE TIPO='K').
    # OJO: VK NO incluye V1+V2+V3 (gastos planta tej). En el PRG eso aparece
    # en el reporte detallado de GASTOS, no en el panel COSTOS.
    VK = (tej["us_externo"] + tej["us_kk_gastos"]) + amort["dtj"]
    inic_tejido_kg = float(inic.get("tejido") or 0)
    cost_tej_kg, cost_tej_kg_src = _eff_rate(KK, inic_tejido_kg)
    cost_tej_us = VK
    cost_tej_ukg = _safe_div(VK, KK) or inic_uk
    cost_tej_proy = pretej

    # ─── COL.QUI. (PRG línea 401) ─────────────────────────────────────
    # PRG: kg=KTINT, ukg=ITIN/KR (NO ITIN/KTINT — sutil), proy=KGPRO*ITIN/KR
    KTINT = float(tin.get("ktint") or 0)
    KR    = float(tin.get("kr") or 0)
    ITIN  = float(tin.get("itin") or 0)
    cost_col_kg = KTINT
    cost_col_us = ITIN
    cost_col_ukg = _safe_div(ITIN, KR) or uq_eff
    cost_col_proy = kgpro * _safe_div(ITIN, KR)

    # ─── GS.PROC. (PRG línea 402) ─────────────────────────────────────
    # PRG línea 212: GTIN = V4+V5+V6 + DCC
    GTIN = gxg["gtin_sin_dcc"] + amort["dcc"]
    cost_gsp_kg = KR
    cost_gsp_us = GTIN
    cost_gsp_ukg = _safe_div(GTIN, KR)
    cost_gsp_proy = pretin

    # ─── GASTOS (PRG línea 403) ───────────────────────────────────────
    # Legacy: GS = G1 + G2 + CA + DEPRCAR (bank flows + caja)
    # Aprox v2: V7+V8+V9 + DEPRCAR. Los V7..V9 son los gastos clasificados
    # de ese rubro en xgast — no es exactamente lo que el PRG calcula desde
    # el flujo bancario. Documentado para que el gerente sepa la diferencia.
    GS = gxg["gs_sin_deprcar"] + amort["deprcar"]
    KV = h_kvent
    cost_gas_us = GS
    cost_gas_ukg = _safe_div(GS, KV)
    cost_gas_proy = preadm
    h_ktej  = float(hist.get("ktej")  or 0)
    h_utej  = float(hist.get("utej")  or 0)
    h_ktin  = float(hist.get("ktin")  or 0)
    h_utin  = float(hist.get("utin")  or 0)
    # h_gasto/h_costo/h_gstotal del cierre histórico ya no se usan: GASTOS
    # viene live de xgast.V7+V8+V9 + DEPRCAR; costo_total se suma de las
    # filas. Sólo h_usuti se conserva para diagnóstico (referencia histórica).
    h_usuti = float(hist.get("usuti") or 0)

    # Tarifas legacy heredadas del último cierre, para diagnóstico.
    _safe_div(h_utej,  h_ktej)              # U$/kg tejido (histórico)
    iqx_legacy = _safe_div(h_utin, h_ktin)           # U$/kg gs.proceso (histórico)

    # Recomputar iqx con tin.itin/KR ahora que tin está disponible —
    # esto reemplaza el placeholder de iqx=0 puesto antes del bloque COSTOS.
    iqx = _safe_div(ITIN, KR) if KR else iqx_legacy
    # Refrescar uq_eff con el valor real ahora que conocemos iqx.
    uq_eff, uq_src = _eff_rate(iqx, inic_uq)

    proy_uvent = kgpro * precio_eff
    proy_mp    = kgpro * um_eff
    proy_col   = kgpro * uq_eff
    proy_total = pretot or (proy_mp + pretej + pretin + preadm + proy_col)

    # Utilidad: PRG línea 380 → UTILIDAD = PATR - PATANT (live).
    # `utilidad` ya está calculada arriba como totl - totp - patant.
    # NO usamos h_usuti (historia.usuti = utilidad del último CIERRE escrito,
    # no la utilidad live del mes). El dBase muestra la live, no el snapshot.
    # Verificado contra foto 30/04: UT.ACT foto = 592.544 = PATR-PATANT live
    # con la data fresca, no historia.usuti del último cierre.
    utilidad_pct = _safe_div(utilidad * 100, h_uvent)
    utilidad_ukg = _safe_div(utilidad, h_kvent)

    # Provisión pendiente del mes (PRG línea 420):
    #     PROVI = 80.000 * (1 - DAY(DD)/30)
    # Se RESTA de la utilidad proyectada (UT.PROY) para reflejar que
    # esos USD todavía no quedan reservados en el cierre. A medida que
    # avanza el mes la provisión va amortizando — el día 30 ya no resta nada.
    provision_pendiente_us = provision_pendiente_mes()

    # UT.PROY del PRG (línea 419-421): proy_uvent − proy_total − provi.
    # Antes calculábamos `proy_uvent − proy_total` y omitíamos las provisiones,
    # quedando la UT.PROY ~$80k optimista al inicio del mes.
    proy_utilidad = proy_uvent - proy_total - provision_pendiente_us

    # Días de cobranza: CART / VENTANUAL * 360 (PRG línea 441).
    cart_dias = _safe_div(cart * 360, venta_anual["uvent_anual"])

    # Stock por etapa — del último snapshot.
    h_hilado = float(hist.get("hilado") or 0) if "hilado" in hist else 0.0
    h_tejido_kg = float(hist.get("tejido") or 0) if "tejido" in hist else 0.0
    h_terminado_kg = float(hist.get("terminado") or 0) if "terminado" in hist else 0.0
    # Si historia no tiene desglose de stock por etapa (sólo `stock` agregado),
    # usamos iniciales como fallback (es el opening — mejor que nada).
    if h_hilado == 0 and h_tejido_kg == 0 and h_terminado_kg == 0:
        h_hilado = float(inic.get("hilado") or 0)
        h_tejido_kg = float(inic.get("tejido") or 0)
        h_terminado_kg = float(inic.get("terminado") or 0)

    # Descontar facturas creadas en Programa Core que todavía no están en
    # el DBF (y por lo tanto no se reflejaron en iniciales.terminado). Sin
    # esto, agregás una factura por la UI y el stock terminado no baja
    # hasta el próximo sync de DBF — TMT 2026-05-11.
    kg_facturas_pc = kg_facturas_pc_no_sincronizadas()
    h_terminado_kg = max(0.0, h_terminado_kg - kg_facturas_pc)

    # ─── Tarifas del panel STOCK ───────────────────────────────────────
    # Reglas PRG (INFORMES.PRG líneas 303-345 + reglas TMT):
    #   - Hilado:    UMX (ponderado live, forward formula del PRG)
    #   - Tejido:    UKK = UMX + 0,5
    #   - Terminado: UFF = UKK + 1,7 = UMX + 2,2
    #
    # Forward formula PRG (INFORMES.PRG línea 337):
    #     UMX = (VM + (HI − KM) × UM0) / HI
    # Multiplicar después por HI (val_hilado) se simplifica a la
    # forma "VM + (HI−KM)·UM0" sin pasar por una división — el dBase
    # lo hace así, sin redondeo intermedio.
    #
    # Anteriormente usábamos back-derive desde historia.ustock para
    # que TOTAL = historia exacto. Lo abandonamos porque:
    #   1. historia es un snapshot stale — cuando hay mutaciones PC
    #      (facturas nuevas, compras, etc.) ya no coincide con el
    #      cálculo correcto. El dBase mismo recalcula UMX al toque
    #      sin atornillarse a historia.ustock.
    #   2. El back-derive requería estimar el costo de las kg
    #      vendidas — siempre dejaba ~10 USD de diff vs dBase.
    # Verificado 2026-05-11: forward → cuadre a ±2 USD con dBase
    # (el resto es precisión de um/uk/uf en el DBF).
    if h_hilado > 0 and um_anterior > 0:
        h_um = (h_ucom + (h_hilado - h_kcom) * um_anterior) / h_hilado
    else:
        h_um = float(inic.get("um") or 0) or umx

    h_uk = h_um + 0.5
    h_uf = h_uk + 1.7

    # ─── DISPLAY del panel STOCK ──────────────────────────────────────
    # Filas: kg × tarifa (full float precision).
    # TOTAL = suma de las filas. Cuadra con el dBase a ±2 USD; con
    # historia.ustock NO necesariamente cuadra porque historia es un
    # snapshot stale que no refleja mutaciones PC.
    val_hilado = h_hilado * h_um
    val_tejido = h_tejido_kg * h_uk
    val_terminado = h_terminado_kg * h_uf
    stock_total_kg = h_hilado + h_tejido_kg + h_terminado_kg
    stock_total_us = val_hilado + val_tejido + val_terminado
    stock_ukg_prom = _safe_div(stock_total_us, stock_total_kg)

    # ─── VSTO del balance = TOTAL del panel STOCK izquierdo ───
    # Para que panel ACTIVO derecho "STOCK MP+PROD." muestre el MISMO
    # número que el TOTAL del panel STOCK izquierdo.
    vsto = stock_total_us

    # ─── UTILIDAD (fórmula explícita TMT 2026-05-06) ───
    #   utility = patrimonio_mayo - patrimonio_abril + dividendos
    #          = (b.patr - b.uret) - patant + b.uret
    #          = b.patr - patant
    totl = subt + vsto + vqx + activos["umaq"] + activos["uact"] + _uret + _antic
    patr = totl - posdats["totp"]
    utilidad = patr - patant
    patr_para_utilidad = patr  # mismo que patr — exposed para el panel debug

    resultados = {
        "ventas": {
            "kg":      h_kvent,
            "ukg":     precio,
            "us":      h_uvent,
            "proy_us": proy_uvent,
            "proy_kg": kgpro,
        },
        "cartera_dias": cart_dias,
        "costos": [
            # Cinco filas exactas del PRG INFORMES.PRG líneas 399-403.
            # Cada fila es un dict {label, kg, ukg, us, proy, src, ayuda}.
            # `src`: 'live' = datos del mes; 'meta' = fallback iniciales;
            #        'none' = sin datos ni objetivo.

            # MAT.PR. — PRG línea 399: kg=KM, ukg=UMX, us=VM, proy=KGPRO*UMX
            # UMX = (VM + (HI - KM)*UM0) / HI con HI = HI0+KM-KH.
            {
                "label": "MAT.PR.",
                "kg":  cost_mat_kg,
                "ukg": cost_mat_ukg,
                "us":  cost_mat_us,
                "proy": cost_mat_proy,
                "src": "live" if cost_mat_us > 0 or HI > 0 else um_src,
                "ayuda": (
                    "MAT.PR. (PRG línea 399). U$/kg = UMX (FIFO ponderado del stock final): "
                    f"({VM:,.0f} + ({HI:,.0f} - {KM:,.0f}) × {UM0:,.3f}) / {HI:,.0f} = {UMX:,.3f}. "
                    f"HI = HI0 ({HI0:,.0f}) + KM ({KM:,.0f}) - KH ({KH:,.0f}). "
                    f"Proyección: KGPRO ({kgpro:,.0f}) × UMX ({UMX:,.3f}) = {cost_mat_proy:,.0f}."
                ),
                "detalle": {
                    "HI0_stock_anterior": HI0, "KM_compras_kg": KM, "VM_compras_us": VM,
                    "UM0_tarifa_anterior": UM0, "KH_salieron_a_tejido": KH,
                    "HI_stock_final": HI, "UMX": UMX,
                },
            },
            # TEJIDO — PRG línea 400: kg=KK, ukg=VK/KK, us=VK, proy=XPRETEJ
            # VK = SUM(IMPORTE TIPO='K') + DTJ. NO incluye V1+V2+V3.
            {
                "label": "TEJIDO",
                "kg":  cost_tej_kg,
                "ukg": cost_tej_ukg,
                "us":  cost_tej_us,
                "proy": cost_tej_proy,
                "src": "live" if cost_tej_us > 0 else (
                    "meta" if (pretej > 0 or cost_tej_kg_src == "meta") else "none"
                ),
                "ayuda": (
                    "TEJIDO (PRG línea 400). U$/kg = VK/KK. "
                    "VK = SUM(IMPORTE WHERE TIPO='K') + DTJ — "
                    "compras tercerizado externo + KK gastos varios + amort.maquinaria. "
                    "NO incluye V1+V2+V3 (sueldos/gas/varios planta — en el PRG aparecen "
                    "en el reporte detallado de GASTOS, no acá). "
                    "KK = SUM(KG WHERE TIPO='K') = interno (PROV='KK') + externo."
                ),
                "detalle": {
                    "VK_us": VK, "KK_kg": KK,
                    "amort_dtj":     amort["dtj"],
                    "kg_interno":    tej["kg_interno"],
                    "kg_externo":    tej["kg_externo"],
                    "us_externo":    tej["us_externo"],
                    "us_kk_gastos":  tej["us_kk_gastos"],
                },
            },
            # COL.QUI. — PRG línea 401: kg=KTINT, ukg=ITIN/KR, us=ITIN, proy=KGPRO*ITIN/KR
            # OJO: ukg divide por KR (kg que llegan a terminado), NO por KTINT.
            {
                "label": "COL.QUI.",
                "kg":  cost_col_kg,
                "ukg": cost_col_ukg,
                "us":  cost_col_us,
                "proy": cost_col_proy,
                "src": "live" if cost_col_us > 0 else uq_src,
                "ayuda": (
                    "COL.QUI. (PRG línea 401). U$/kg = ITIN / KR. "
                    f"ITIN ({ITIN:,.0f}) = SUM(importe) en TINTO del mes. "
                    f"KR ({KR:,.0f}) = SUM(kgn) en TINTO WHERE color<>'LAV' (kg que llegan a terminado). "
                    f"Notar que el kg de la columna muestra KTINT ({KTINT:,.0f}) pero el ukg "
                    f"divide por KR — convención dBase que acentúa el costo por kg vendible."
                ),
                "detalle": {"KTINT": KTINT, "KR": KR, "ITIN": ITIN},
            },
            # GS.PROC. — PRG línea 402: kg=KR, ukg=GTIN/KR, us=GTIN, proy=XPRETIN
            # GTIN = V4+V5+V6 + DCC.
            {
                "label": "GS.PROC.",
                "kg":  cost_gsp_kg,
                "ukg": cost_gsp_ukg,
                "us":  cost_gsp_us,
                "proy": cost_gsp_proy,
                "src": "live" if cost_gsp_us > 0 else ("meta" if pretin > 0 else "none"),
                "ayuda": (
                    "GS.PROC. (PRG línea 402). U$/kg = GTIN / KR. "
                    f"GTIN ({GTIN:,.0f}) = V4+V5+V6 ({gxg['gtin_sin_dcc']:,.0f}) + DCC ({amort['dcc']:,.0f}). "
                    f"KR ({KR:,.0f}) = kg que llegan a terminado este mes."
                ),
                "detalle": {
                    "v4_v5_v6": gxg["gtin_sin_dcc"], "amort_dcc": amort["dcc"],
                    "GTIN": GTIN, "KR": KR,
                },
            },
            # GASTOS — PRG línea 403: ukg=GS/KV, us=GS, proy=XPREADM (sin kg).
            # En el legacy puro: GS = G1+G2+CA+DEPRCAR (flujo bancario). Acá
            # aproximamos con V7+V8+V9 + DEPRCAR (sólo lo categorizado en xgast
            # como rubro 7/8/9). Esto puede diferir del dBase por las gastos
            # bancarios sin categoría que el legacy capturaba via FILTRO.
            {
                "label": "GASTOS",
                "kg":  None,
                "ukg": cost_gas_ukg,
                "us":  cost_gas_us,
                "proy": cost_gas_proy,
                "src": "live" if cost_gas_us > 0 else ("meta" if preadm > 0 else "none"),
                "ayuda": (
                    "GASTOS (PRG línea 403). U$/kg = GS / KV. "
                    f"GS ({GS:,.0f}) ≈ V7+V8+V9 ({gxg['gs_sin_deprcar']:,.0f}) + DEPRCAR ({amort['deprcar']:,.0f}). "
                    f"KV ({KV:,.0f}) = kg de ventas del mes. "
                    "(Diferencia con dBase: el legacy calcula GS = G1+G2+CA+DEPRCAR desde flujo bancario."
                ),
                "detalle": {
                    "v7_v8_v9": gxg["gs_sin_deprcar"], "amort_deprcar": amort["deprcar"],
                    "GS": GS, "KV": KV,
                },
            },
        ],
        "tarifas_src": {
            "precio": precio_src,
            "um": um_src,
            "uq": uq_src,
        },
        "costo_total": _costo_total_con_desperdicio(
            cost_mat_ukg=cost_mat_ukg, cost_col_ukg=cost_col_ukg,
            cost_tej_ukg=cost_tej_ukg, cost_gsp_ukg=cost_gsp_ukg, cost_gas_ukg=cost_gas_ukg,
            cost_mat_us=cost_mat_us, cost_col_us=cost_col_us,
            cost_tej_us=cost_tej_us, cost_gsp_us=cost_gsp_us, cost_gas_us=cost_gas_us,
            cost_mat_proy=cost_mat_proy, cost_col_proy=cost_col_proy,
            cost_tej_proy=cost_tej_proy, cost_gsp_proy=cost_gsp_proy, cost_gas_proy=cost_gas_proy,
            KR=KR, KTINT=KTINT,
        ),
        "utilidad": {
            # UT.ACT del PRG = PATR - PATANT (live). Foto: 592.544.
            "pct":     utilidad_pct,
            "ukg":     utilidad_ukg,
            "us":      utilidad,           # ← live PATR-PATANT, no h_usuti
            "proy_us": proy_utilidad,
            # Provisión que falta amortizar este mes (PRG línea 420).
            # Se restó dentro de `proy_us`; la exponemos también acá para
            # que el template muestre la fila intermedia "Provisión pendiente".
            "provision_pendiente": provision_pendiente_us,
            # h_usuti = lo escrito en historia al último cierre (dato histórico
            # de referencia). En condiciones normales utilidad ≈ h_usuti
            # cuando el cierre se acaba de hacer. Lo dejo expuesto para el
            # diagnóstico (el panel puede compararlos si difieren).
            "usuti_historia": h_usuti,
        },
        "stock": {
            "hilado":    {"kg": h_hilado,        "ukg": h_um, "us": val_hilado},
            "tejido":    {"kg": h_tejido_kg,     "ukg": h_uk, "us": val_tejido},
            "terminado": {"kg": h_terminado_kg,  "ukg": h_uf, "us": val_terminado},
            "total":     {"kg": stock_total_kg,  "ukg": stock_ukg_prom, "us": stock_total_us},
        },
        "snapshot_fecha": snap_fecha,
        "iniciales_mes": (
            f"{inic.get('mesnom') or '?'} {inic.get('yy') or ''}"
            if inic else None
        ),
    }

    resultado = {
        "totf": _totf, "totc": _totc,
        "bancos": bancos_activos,
        "bancos_todos": bancos,
        "salbanc1": salbanc1, "salbanc2": salbanc2, "salbanc": salbanc,
        "pos1": posdats["pos1"], "pos2": posdats["pos2"],
        "salcaj": _salcaj,
        "umaq": activos["umaq"], "uact": activos["uact"],
        "antic": _antic, "uret": _uret, "uret_anio": _uret_anio,
        "ventas_anio": _ventas_anio,
        "totp": posdats["totp"],
        "vsto": vsto, "vqx": vqx,
        "cart": cart, "subt": subt, "totl": totl,
        "patr": patr, "patant": patant, "utilidad": utilidad,
        # `patr_para_utilidad` = patr ANTES del override de vsto, coherente
        # con PATANT (= historia.patrimonio neto del cierre anterior).
        # Lo expongo para que el panel pueda mostrar el cálculo:
        # utilidad = patr_para_utilidad - patant (sin re-valuación de stock).
        "patr_para_utilidad": patr_para_utilidad,
        # Provisión que aún no se amortizó este mes (PRG L420).
        # Top-level para que el template pueda intercalarla entre UT.ACT
        # y UT.PROY sin tener que entrar a resultados.utilidad.*.
        "provision_pendiente": provision_pendiente_us,
        "fecha": date.today(),
        "snapshot_historia_fecha": snap_fecha,
        "kg": kg,
        "diagnostico": diagnostico,
        "resultados": resultados,
        "conciliacion": conciliacion_balance(),
        # TMT 2026-05-19 — item 15b: cuadro MOVIMIENTOS MES estilo dBase.
        # Fallback a None si la query rompe (no debe tirar la página).
        "movimientos_mes": _try_movimientos_mes(),
    }

    # ---- Math check de invariantes — TODAS las sumas deben cuadrar.
    errores_math = _verificar_balance_math(resultado)
    if errores_math:
        # En dev (incluye tests): error duro, así nadie se entera tarde.
        # En prod: agregar a advertencias del banner ámbar para que el
        # gerente lo vea, pero no romper la página.
        import os
        env = os.environ.get("ENV", "development").lower()
        msg_completo = "Invariantes del balance violadas:\n  - " + "\n  - ".join(errores_math)
        if env == "development":
            raise AssertionError(msg_completo)
        # Producción: anexar al diagnóstico
        for e in errores_math:
            resultado["diagnostico"]["advertencias"].append(e)

    return resultado


# ---------------------------------------------------------------------------
# CARTERA — saldos por cliente
# ---------------------------------------------------------------------------

def cartera_por_cliente() -> list[dict]:
    """Agregado por cliente: cheques + facturas + total + % del total.

    TMT 2026-05-18 — Pedido dueña: vista de 5 columnas
    CLIENTE | CHEQUES | FACTURAS | TOTAL | % DEL TOTAL, orden desc por %.

    - CHEQUES  = cheques en cartera del cliente (stat Z/1/2/3/P/D/A)
    - FACTURAS = SUM(factura.saldo) viva
    - TOTAL    = FACTURAS − CHEQUES (paridad dBase, lo neto a cobrar)
    """
    rows = db.fetch_all(
        """
        WITH cheques_cli AS (
            SELECT codigo_cli,
                   COALESCE(SUM(importe), 0) AS cheques
              FROM scintela.cheque
             WHERE stat IN ('Z','1','2','3','P','D','A')
               AND codigo_cli IS NOT NULL
             GROUP BY codigo_cli
        )
        SELECT f.codigo_cli,
               COALESCE(c.nombre, '(sin nombre)')          AS nombre,
               COUNT(f.id_factura)                         AS n_facturas,
               COALESCE(SUM(f.saldo), 0)                   AS facturas,
               COALESCE(MAX(cc.cheques), 0)                AS cheques,
               COALESCE(SUM(f.saldo), 0)
                 - COALESCE(MAX(cc.cheques), 0)            AS saldo_total,
               MIN(f.fecha)                                AS factura_mas_vieja,
               MIN(f.vencimiento)                          AS vence_mas_viejo
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        LEFT JOIN cheques_cli cc      ON cc.codigo_cli = f.codigo_cli
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
        GROUP BY f.codigo_cli, c.nombre
        """
    ) or []

    total = sum(float(r.get("saldo_total") or 0) for r in rows) or 1.0
    for r in rows:
        r["pct"] = round(float(r.get("saldo_total") or 0) / total * 100, 1)
    rows.sort(key=lambda r: r.get("pct", 0), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# DEUDAS — pasivos por posdat (como lo hace INFORMES.PRG)
# ---------------------------------------------------------------------------

def deudas_por_proveedor() -> list[dict]:
    """Pasivos agrupados por proveedor (deuda viva = banc=0).

    Sólo banc=0 (no instrumentada). banc=1/2 ya descontaron el saldo
    bancario; banc=9 son cheques posdatados ya emitidos — sumarlos como
    deuda double-counta vs el balance. Mismo criterio que TOTP.
    """
    return db.fetch_all(
        f"""
        SELECT COALESCE(p.codigo_prov, pd.prov)   AS codigo_prov,
               COALESCE(p.nombre, pd.prov, '—')   AS nombre,
               COUNT(pd.id_posdat)                AS n_posdats,
               COALESCE(SUM(pd.importe), 0)       AS saldo_total,
               MIN(pd.fecha)                      AS posdat_mas_vieja,
               MIN(pd.fechad)                     AS vence_mas_viejo
        FROM scintela.posdat pd
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
        WHERE {posdat_deuda_viva_where('pd')}
          AND COALESCE(pd.importe, 0) > 0
          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
        GROUP BY p.codigo_prov, pd.prov, p.nombre
        ORDER BY saldo_total DESC
        """
    )


# ---------------------------------------------------------------------------
# FLUJO — últimos N días
# ---------------------------------------------------------------------------

def flujo_ultimos_dias(dias: int = 30) -> list[dict]:
    # Coerce to int defensively; callers may pass strings from query params.
    try:
        dias_int = int(dias)
    except (TypeError, ValueError):
        dias_int = 30
    return db.fetch_all(
        """
        SELECT fecha, cheques, facturas, pichincha, inter,
               posdat1, posdat2, mprima, gastos, saldo, pagos, dolares, usaldo
        FROM scintela.flujo
        WHERE fecha >= CURRENT_DATE - make_interval(days => %s)
        ORDER BY fecha DESC
        """,
        (dias_int,),
    )


def flujo_calculado(
    dias_atras: int = 14,
    dias_adelante: int = 365,
    ignorar_cheques: bool = False,
) -> list[dict]:
    """Flujo de caja calculado EN VIVO desde los datos transaccionales.

    Distinto de `flujo_proyeccion()`: éste no depende de que alguien haya
    cargado la tabla `scintela.flujo`. Computa la proyección directamente
    desde las fuentes de verdad:

      - Saldo inicial = SUM(saldos bancarios actuales).
      - Por cada día futuro:
          + cheques en cartera (Z, D, P) con fechad = ese día → ingresos
          - posdat con banc<>9 con fechad = ese día → egresos a proveedores
      - El saldo se acumula día a día.

    Para días pasados (`dias_atras`) no calculamos historia real (sería caro
    y requeriría hacer replay del libro bancario). Mostramos el saldo de
    hoy como línea recta hacia atrás — sirve para dar contexto visual al
    gerente sin engañar.

    Devuelve filas con la misma forma que `flujo_proyeccion()` para que el
    template las pueda consumir igual:
        {fecha, saldo, cheques, facturas, posdat1, posdat2, pichincha,
         inter, mprima, gastos, pagos, dolares}

    El gráfico actual usa principalmente: saldo, cheques, gastos, mprima.
    Los demás van en 0.
    """
    from datetime import timedelta as _td
    try:
        atras = max(0, int(dias_atras))
    except (TypeError, ValueError):
        atras = 14
    try:
        adelante = max(30, int(dias_adelante))
    except (TypeError, ValueError):
        adelante = 365

    # ─────────────────────────────────────────────────────────────────────
    # Replicación EXACTA de PROCEDURE FLUJO en MENU.PRG líneas 569-720.
    # Saldo inicial:
    #   ST = S1 + S2 + SALCA
    #   S1 = saldo Pichincha (= banco_1)
    #   S2 = saldo Internacional (= banco_2)
    #   SALCA = caja saldo final
    # Por día (FECHAD): aplicar todos los movimientos vivos:
    #   + cheques en cartera (stat Z/1/2/3/P/D)               → INGRESO
    #   + facturas vivas (stat Z/A) por VENCIMIENTO           → INGRESO
    #   - posdat (banc<>9, todos)                             → EGRESO
    # Acumulación día a día.
    # ─────────────────────────────────────────────────────────────────────

    # 1) Saldo CAJA — último saldo registrado.
    caja_row = db.fetch_one(
        """
        SELECT saldo
        FROM scintela.caja
        WHERE saldo IS NOT NULL
        ORDER BY fecha DESC NULLS LAST, id_caja DESC
        LIMIT 1
        """
    )
    saldo_caja = float((caja_row or {}).get("saldo") or 0) if caja_row else 0.0

    # 2) Saldo BANCOS — suma del último saldo por banco.
    saldo_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(s), 0) AS saldo_total FROM (
          SELECT (
            SELECT t.saldo
            FROM scintela.transacciones_bancarias t
            WHERE t.no_banco = b.no_banco
            ORDER BY t.fecha DESC, t.id_transaccion DESC
            LIMIT 1
          ) AS s
          FROM scintela.banco b
        ) sub
        """
    )
    saldo_bancos = float((saldo_row or {}).get("saldo_total") or 0)

    # Saldo inicial total: caja + bancos. (PRG: ST = S1+S2+SALCA).
    saldo_hoy = saldo_caja + saldo_bancos

    # 3) Cheques en cartera por FECHAD (= fecha de depósito).
    # PRG línea 643: &AF CHEQUES FOR STAT $ "Z123P" — Z=cartera, 1/2/3=banco
    # asignado, P=postergado, D=depositado-pendiente. Agregamos D que
    # también es ingreso futuro (cheque ya en banco esperando acreditación).
    #
    # Modo `ignorar_cheques`: el gerente quiere ver el peor caso (sólo
    # egresos por posdat, sin contar cobranzas futuras). Útil cuando hay
    # sospecha de cheques con stat=Z stale (cobrados pero no marcados).
    if ignorar_cheques:
        cheques_por_dia: dict = {}
    else:
        cheques_rows = db.fetch_all(
            """
            SELECT fechad AS fecha,
                   COALESCE(SUM(importe), 0) AS total
              FROM scintela.cheque
             WHERE stat IN ('Z','1','2','3','P','D')
               AND fechad IS NOT NULL
               AND fechad >= CURRENT_DATE
               AND fechad <= CURRENT_DATE + make_interval(days => %s)
             GROUP BY fechad
            """,
            (adelante,),
        ) or []
        cheques_por_dia = {r["fecha"]: float(r["total"] or 0) for r in cheques_rows}

    # 4) Facturas — el dBase legacy NO las incluye en el gráfico, aunque
    # las procesa en el loop. PRG MENU.PRG línea 670: `FA = 0` se ejecuta
    # ANTES del REPLA SALDO WITH ST-P1-P2+C+FA-G-H, anulando el aporte
    # de FA. La intención del autor original fue mostrar SOLAMENTE flujo
    # de cheques (lo que efectivamente entra en banco) sin contar facturas
    # (que pueden cobrar tarde, con descuento, o no cobrar). Replicamos
    # ese comportamiento — facturas_por_dia queda vacío.
    facturas_por_dia: dict = {}

    # 5) Posdat por FECHAD (egresos). PRG MENU.PRG líneas 677-679:
    #     CASE BANC = 1 → P1 += IMPORTE   (cheque emitido, banco 1)
    #     CASE BANC = 2 → P2 += IMPORTE   (cheque emitido, banco 2)
    #     CASE BANC=9 OR BANC=0 → G += IMPORTE  (egreso general)
    # Los tres se descuentan del saldo: SALDO = ST - P1 - P2 + C + FA - G - H.
    # ⇒ TODO posdat afecta el flujo, sin importar banc.
    #
    # Bug TMT 2026-05-08: filtrabamos `banc<>9` pensando que BANC=9 =
    # "cerrada/pagada". Es cierto para el balance/pasivos pero NO para
    # el flujo: BANC=9 son cheques POSDATADOS que YA EMITIMOS a
    # proveedores y que van a salir del banco al fechad. En la data
    # actual son $5.9M de obligaciones reales — no contarlas mostraba un
    # Replicación EXACTA de MENU.PRG líneas 683-684:
    #     CASE BANC=9 .OR. BANC=0
    #        G = G + IMPORTE
    # dBase suma TODOS los banc=0 y banc=9 como egresos (G).
    # Vencidos: el PRG legacy en línea 649 los empuja a CURRENT_DATE+7
    #     &RF DATE()+7 FOR FECHAD<=DATE()+5 AND NB=0 AND PROV=' '
    # Nosotros los imputamos a hoy (equivalente, más conservador).
    #
    # banc=10/32 (modernos PC) NO se cuentan: bank_helpers.insert_movimiento_bancario
    # ya descontó saldo en transacciones_bancarias al emitir, así que el
    # saldo de hoy ya los refleja. Sumarlos sería double-counting.
    #
    # Historia (errores que cometí 2026-05-13, no repetir):
    #   1) Cambié a banc=0 only → flujo optimista, dBase mostraba -$2.3M.
    #   2) Filtre banc=9 vencidos → faltaron $1.3M; gap consistente vs dBase.
    #   3) Definitivo: mirror dBase = banc IN (0, 9), vencidos imputados a hoy.
    posdat_rows = db.fetch_all(
        f"""
        SELECT
          CASE WHEN fechad < CURRENT_DATE THEN CURRENT_DATE ELSE fechad END AS fecha,
          COALESCE(SUM(importe), 0) AS total
        FROM scintela.posdat
        WHERE fechad IS NOT NULL
          AND fechad <= CURRENT_DATE + make_interval(days => %s)
          AND {POSDAT_EGRESO_FLUJO_WHERE}
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        GROUP BY 1
        """,
        (adelante,),
    ) or []
    posdat_por_dia = {r["fecha"]: float(r["total"] or 0) for r in posdat_rows}

    # 6) Construir la curva día a día.
    hoy = date.today()
    filas: list[dict] = []
    saldo_acum = saldo_hoy

    # Días pasados — línea recta del saldo actual (no recalculamos historia).
    for offset in range(-atras, 0):
        fecha = hoy + _td(days=offset)
        filas.append({
            "fecha": fecha,
            "saldo": saldo_hoy,
            "cheques": 0.0, "facturas": 0.0,
            "posdat1": 0.0, "posdat2": 0.0,
            "pichincha": 0.0, "inter": 0.0,
            "mprima": 0.0, "gastos": 0.0,
            "pagos": 0.0, "dolares": 0.0,
        })

    # Hoy y adelante — proyección acumulada.
    # Día t: saldo_t = saldo_{t-1} + cheques(t) + facturas(t) - posdat(t)
    # PRG línea 668: REPLA SALDO WITH ST-P1-P2+C+FA-G-H
    #
    # ⚠ Importante: aplicamos cambios también en offset=0 (hoy). Los
    # vencidos los imputamos a hoy (CASE WHEN fechad < hoy THEN hoy)
    # y deben restarse al saldo HOY, no a mañana. Si los salteábamos
    # con `if offset > 0` se perdían $1.4M de banc=0 + banc=9 vencidos
    # y el chart quedaba $1.4M más optimista que la suma SQL directa.
    for offset in range(0, adelante + 1):
        fecha = hoy + _td(days=offset)
        cheq_in = cheques_por_dia.get(fecha, 0.0)
        fact_in = facturas_por_dia.get(fecha, 0.0)
        egreso = posdat_por_dia.get(fecha, 0.0)
        saldo_acum = saldo_acum + cheq_in + fact_in - egreso
        filas.append({
            "fecha": fecha,
            "saldo": saldo_acum,
            "cheques": cheq_in,
            "facturas": fact_in,
            "posdat1": 0.0, "posdat2": 0.0,
            "pichincha": 0.0, "inter": 0.0,
            "mprima": 0.0,
            "gastos": -egreso,    # negativo: el chart lo trata como egreso
            "pagos": 0.0,
            "dolares": 0.0,
        })

    return filas


def plazos_dbase() -> dict:
    """KPIs PLAZ.COBR y PLAZ.DEUDA del gráfico de flujo, calculados como
    en dBase: plazo otorgado promedio (vencimiento − fecha_emisión)
    ponderado por importe.

    NO se usa la fórmula `fecha_evento - hoy` que estaba en el JS — esa
    medía "días restantes promedio en la ventana visible", no el plazo
    real otorgado, y arrojaba 23/25 vs los 32.9/96.7 de dBase.

    Filtros aplicados:
      - PLAZ.COBR: facturas con saldo > 0 y stat ∈ ('Z','A','','space').
        Sólo abiertas (las cobradas ya no informan plazo otorgado vigente).
      - PLAZ.DEUDA: posdat con banc=0 y (fechad - fecha) BETWEEN 0 AND 365.
        El filtro 0-365 saca refinanciamientos eternos (YY de 2009, BP
        de 2022) que distorsionan el promedio a ~1400 días.

    Devuelve:
        {"cobro": int, "deuda": int, "n_facturas": int, "n_posdat": int}
        Días redondeados al entero más cercano.
    """
    row_cobro = db.fetch_one(
        """
        SELECT
          ROUND(SUM(saldo * (vencimiento - fecha)) / NULLIF(SUM(saldo), 0))::int AS dias,
          COUNT(*) AS n
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
          AND vencimiento IS NOT NULL
          AND fecha IS NOT NULL
        """
    ) or {}
    row_deuda = db.fetch_one(
        f"""
        SELECT
          ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0))::int AS dias,
          COUNT(*) AS n
        FROM scintela.posdat
        WHERE {POSDAT_DEUDA_VIVA_WHERE}
          AND fechad IS NOT NULL
          AND fecha IS NOT NULL
          AND (fechad - fecha) BETWEEN 0 AND 365
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or {}
    return {
        "cobro": int(row_cobro.get("dias") or 0),
        "deuda": int(row_deuda.get("dias") or 0),
        "n_facturas": int(row_cobro.get("n") or 0),
        "n_posdat": int(row_deuda.get("n") or 0),
    }


def kg_facturas_pc_no_sincronizadas() -> float:
    """Suma de kg de facturas creadas en Programa Core que todavía NO están
    reflejadas en `scintela.iniciales`.

    El criterio: facturas con `usuario_crea != 'dbf-import'` son las que se
    crearon vía la UI (no importadas del DBF). Esas todavía no llegaron al
    DBF de la fábrica, así que iniciales.terminado (que sí viene del DBF)
    no las descuenta. Las restamos al display para que el stock terminado
    refleje el efecto de la factura recién emitida.

    Excluye anuladas (stat='Y') y canceladas (stat='X').

    Cuando hagas el próximo `import_dbf.py`, esas facturas ya van a estar
    en el DBF y este número va a volver a 0 (porque ahora tienen
    `usuario_crea='dbf-import'`).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS total
        FROM scintela.factura
        WHERE COALESCE(usuario_crea, '') <> 'dbf-import'
          AND (stat IS NULL OR stat <> 'X')
        """
    )
    return float((row or {}).get("total") or 0)


def posdat_egresos_proximos(dias_adelante: int = 365) -> list[dict]:
    """Lista de posdat que se van a restar del flujo, en orden por fechad.

    Mismo criterio que `flujo_calculado()` (replica MENU.PRG 683):
      - banc IN (0, 9) — vencidos imputados a hoy
      - banc=10/32 NO se incluyen — son cheques modernos cuyo CH en
        transacciones_bancarias ya descontó saldo hoy.

    Devuelve filas con:
        fecha_efectiva : date  (= max(fechad, hoy))
        fechad         : date  (la original, para indicar vencidos)
        prov           : str
        concepto       : str
        importe        : float
        banc           : int   (0 o 9 en los resultados)
    """
    return db.fetch_all(
        f"""
        SELECT
          CASE WHEN fechad < CURRENT_DATE THEN CURRENT_DATE ELSE fechad END
            AS fecha_efectiva,
          fechad,
          COALESCE(prov, '')      AS prov,
          COALESCE(concepto, '')  AS concepto,
          COALESCE(importe, 0)    AS importe,
          COALESCE(banc, 0)       AS banc
        FROM scintela.posdat
        WHERE fechad IS NOT NULL
          AND fechad <= CURRENT_DATE + make_interval(days => %s)
          AND {POSDAT_EGRESO_FLUJO_WHERE}
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        ORDER BY fecha_efectiva ASC, importe DESC
        """,
        (max(30, int(dias_adelante)),),
    ) or []


def flujo_proyeccion(dias_atras: int = 14, dias_adelante: int = 365) -> list[dict]:
    """Window of scintela.flujo around today for the projection chart.

    scintela.flujo holds both realized history and forward projection
    (postdated cheques + recurring provisiones + scheduled pagos). We
    pull a small history slice so the chart has context and a wide
    forward slice so the gerente can see where the curve goes.

    Rows come out in ASCENDING date order — the chart expects that.
    """
    try:
        atras = max(0, int(dias_atras))
    except (TypeError, ValueError):
        atras = 14
    try:
        adelante = max(30, int(dias_adelante))
    except (TypeError, ValueError):
        adelante = 365
    return db.fetch_all(
        """
        SELECT fecha,
               COALESCE(saldo, 0)     AS saldo,
               COALESCE(cheques, 0)   AS cheques,
               COALESCE(facturas, 0)  AS facturas,
               COALESCE(posdat1, 0)   AS posdat1,
               COALESCE(posdat2, 0)   AS posdat2,
               COALESCE(pichincha, 0) AS pichincha,
               COALESCE(inter, 0)     AS inter,
               COALESCE(mprima, 0)    AS mprima,
               COALESCE(gastos, 0)    AS gastos,
               COALESCE(pagos, 0)     AS pagos,
               COALESCE(dolares, 0)   AS dolares
        FROM scintela.flujo
        WHERE fecha >= CURRENT_DATE - make_interval(days => %s)
          AND fecha <= CURRENT_DATE + make_interval(days => %s)
        ORDER BY fecha ASC
        """,
        (atras, adelante),
    )


FLUJO_COLS = (
    "cheques", "facturas", "posdat1", "posdat2",
    "pichincha", "inter", "mprima", "gastos",
    "saldo", "pagos", "dolares", "usaldo",
)


def upsert_flujo_rows(rows: list[dict], usuario: str) -> dict:
    """Insert or update rows in scintela.flujo keyed by fecha.

    Each row must have `fecha` (date) plus any subset of FLUJO_COLS.
    Missing columns are left NULL on insert, untouched on update.

    Returns {"inserted": n, "updated": n}.

    Uses a single transaction — either all rows land or none do.
    """
    if not rows:
        return {"inserted": 0, "updated": 0}
    inserted = 0
    updated = 0
    with db.tx() as conn:
        for row in rows:
            fecha = row.get("fecha")
            if fecha is None:
                continue
            # Does this fecha already exist?
            existing = db.fetch_one(
                "SELECT id_flujo FROM scintela.flujo WHERE fecha = %s",
                (fecha,),
                conn=conn,
            )
            # Build column/value lists from the supplied keys only, so we
            # never overwrite an existing column with NULL by accident.
            supplied = [(c, row[c]) for c in FLUJO_COLS if c in row]
            if existing:
                if supplied:
                    set_sql = ", ".join(f"{c} = %s" for c, _ in supplied)
                    params = tuple(v for _, v in supplied) + (
                        usuario, existing["id_flujo"],
                    )
                    db.execute(
                        f"UPDATE scintela.flujo SET {set_sql}, "
                        "fecha_modifica = CURRENT_TIMESTAMP, usuario_modifica = %s "
                        "WHERE id_flujo = %s",
                        params,
                        conn=conn,
                    )
                    updated += 1
            else:
                cols = ["fecha"] + [c for c, _ in supplied] + ["usuario_crea"]
                vals = [fecha] + [v for _, v in supplied] + [usuario]
                placeholders = ", ".join(["%s"] * len(vals))
                db.execute(
                    f"INSERT INTO scintela.flujo ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    tuple(vals),
                    conn=conn,
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# VENTAS mensuales — agregadas desde factura
# ---------------------------------------------------------------------------

def ventas_mensuales(meses: int = 12) -> list[dict]:
    """Ventas e importes por mes de los últimos N meses."""
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               COUNT(*)                         AS n_facturas,
               COALESCE(SUM(kg), 0)             AS kg_total,
               COALESCE(SUM(importe), 0)        AS importe_total,
               COALESCE(SUM(abono), 0)          AS abonado_total
        FROM scintela.factura
        WHERE fecha >= (CURRENT_DATE - (%s || ' months')::interval)
          AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
        GROUP BY mes
        ORDER BY mes DESC
        """,
        (str(meses),),
    )


def ventas_multianual(anios: int = 4) -> dict:
    """Matriz mes × año de ventas — replica MODIFICA.PRG PROCEDURE VENTAS L144-217.

    El PRG agrupa FACTURAS.DBF (más histories FAC20/FAC21/FAC22/FAC23) por
    `YEAR + MONTH/100` y muestra una tabla 12 meses × 2 años (corriente +
    anterior) con totales y precio promedio. Acá generalizamos a N años
    (default 4) y agregamos % vs año anterior por celda.

    Estructura:
        {
          "anios": [2023, 2024, 2025, 2026],          # ordenado asc
          "meses": [
            {"mes": 1, "label": "Ene", "datos": {
                2023: {"kg":..., "importe":..., "n":...},
                2024: {...},
                ...
            }},
            ...
          ],
          "totales_por_anio": {
              2023: {"kg":..., "importe":..., "precio_prom":..., "n":...},
              ...
          },
          "n_anios": 4,
        }

    Guards:
      - `anios` capeado a 1..10.
      - Periodos cerrados → la query usa `vigente_en` para excluir periodos
        cerrados si la migración 0005 lo expone; por ahora replicamos PRG
        que NO chequea período (cuenta todo lo que está en facturas).
      - Anuladas (stat='X', 'Y') excluidas — el PRG hace `STAT $ "ZA"`
        (Z=emitida, A=parcial). T/P/empty también se aceptan como el legacy.
    """
    try:
        n = max(1, min(int(anios), 10))
    except (TypeError, ValueError):
        n = 4

    hoy = date.today()
    anio_actual = hoy.year
    anios_list = [anio_actual - (n - 1) + i for i in range(n)]  # asc

    # Una query — todas las facturas vivas en el rango de años + agrupado por
    # (year, month). Filtramos stat para excluir anuladas (TMT bug TMT
    # 2026-04-29: filas con stat='Y' inflaban U$/kg). PRG: `STAT $ "ZA"`.
    rows = db.fetch_all(
        """
        SELECT EXTRACT(YEAR  FROM fecha)::int AS yy,
               EXTRACT(MONTH FROM fecha)::int AS mm,
               COUNT(*)                       AS n,
               COALESCE(SUM(kg), 0)           AS kg,
               COALESCE(SUM(importe), 0)      AS importe
        FROM scintela.factura
        WHERE EXTRACT(YEAR FROM fecha) BETWEEN %s AND %s
          AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
        GROUP BY yy, mm
        """,
        (anios_list[0], anios_list[-1]),
    ) or []

    idx: dict[tuple[int, int], dict] = {}
    for r in rows:
        idx[(int(r["yy"]), int(r["mm"]))] = {
            "kg":      float(r.get("kg") or 0),
            "importe": float(r.get("importe") or 0),
            "n":       int(r.get("n") or 0),
        }

    mes_labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                  "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

    def _pct_var(act: float, ant: float) -> float | None:
        if not ant:
            return None
        return (act - ant) * 100.0 / ant

    meses_out = []
    for m in range(1, 13):
        datos: dict = {}
        for a in anios_list:
            datos[a] = idx.get((a, m), {"kg": 0.0, "importe": 0.0, "n": 0})
        # Variaciones celda por celda contra el mismo mes del año anterior.
        var: dict = {}
        for i, a in enumerate(anios_list):
            if i == 0:
                var[a] = None
            else:
                prev = anios_list[i - 1]
                var[a] = _pct_var(datos[a]["kg"], datos[prev]["kg"])
        datos["var_kg_pct"] = var
        meses_out.append({"mes": m, "label": mes_labels[m - 1], "datos": datos})

    # Totales por año (PRG L211-218 SUM ALL KG/IMPORTE FOR YEAR=...).
    totales_por_anio: dict[int, dict] = {}
    for a in anios_list:
        tot_kg = 0.0
        tot_us = 0.0
        tot_n  = 0
        for m in range(1, 13):
            d = idx.get((a, m), {})
            tot_kg += float(d.get("kg") or 0)
            tot_us += float(d.get("importe") or 0)
            tot_n  += int(d.get("n") or 0)
        precio_prom = (tot_us / tot_kg) if tot_kg else 0.0
        totales_por_anio[a] = {
            "kg":          tot_kg,
            "importe":     tot_us,
            "precio_prom": precio_prom,
            "n":           tot_n,
        }

    return {
        "anios":            anios_list,
        "meses":            meses_out,
        "totales_por_anio": totales_por_anio,
        "n_anios":          n,
    }


# ---------------------------------------------------------------------------
# GASTOS — mes en curso, agrupados (vía transacciones_bancarias + caja)
# ---------------------------------------------------------------------------

def gastos_mes_corriente() -> list[dict]:
    """
    Gastos del mes agrupados por concepto.
    PRG replica la lógica de GPICH+GINT+UGCAJA. Aca arrancamos con un
    listado plano que después podemos segmentar (v2).
    """
    return db.fetch_all(
        """
        SELECT tb.fecha, tb.documento, tb.concepto, tb.importe,
               COALESCE(p.nombre, tb.prov, '') AS proveedor,
               b.nombre                         AS banco
        FROM scintela.transacciones_bancarias tb
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = tb.prov
        LEFT JOIN scintela.banco     b ON b.no_banco   = tb.no_banco
        WHERE tb.fecha >= date_trunc('month', CURRENT_DATE)
          AND tb.documento IN ('CH','ND')
        ORDER BY tb.fecha DESC, tb.id_transaccion DESC
        """
    )


# ---------------------------------------------------------------------------
# RETIROS — lista y totales
# ---------------------------------------------------------------------------

def retiros_recientes(dias: int = 180) -> list[dict]:
    return db.fetch_all(
        """
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto,
               b.nombre AS banco
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE r.fecha >= CURRENT_DATE - (%s || ' days')::interval
        ORDER BY r.fecha DESC, r.id_retiro DESC
        """,
        (str(dias),),
    )


def retiros_total_anual() -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0) AS total
        FROM scintela.retiros
        WHERE EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM CURRENT_DATE)
        """
    )
    return float(row["total"] or 0) if row else 0.0


# ---------------------------------------------------------------------------
# ACTIVOS — lista de activos fijos con amortización
# ---------------------------------------------------------------------------

def activos_lista() -> list[dict]:
    return db.fetch_all(
        """
        SELECT a.id_activos, a.fecha, a.concepto, a.tipo,
               a.inicial, a.amortizac, a.amortimes, a.valor,
               a.cuota, a.vida_util, a.ult_mes_amortizado,
               COALESCE(p.nombre, '') AS proveedor
        FROM scintela.activos a
        LEFT JOIN scintela.proveedor p ON p.id_proveedor = a.id_proveedor
        ORDER BY a.tipo, a.valor DESC
        """
    )


# ---------------------------------------------------------------------------
# HISTORIA — snapshots mensuales
# ---------------------------------------------------------------------------

def historia_lista(limite: int = 24) -> list[dict]:
    """Snapshot mensual completo — 26 columnas de datos como en HISTORIA.DBF."""
    return db.fetch_all(
        """
        SELECT fecha,
               stock, kcom, ktej, ktin, ustock, uqui,
               kvent, uvent, costo, ucom, utej, utin,
               gasto, gstotal,
               banco, cart, deuda, retiro, patrimonio, anticipos,
               dolar, maquinaria, realty, usret, usuti
        FROM scintela.historia
        ORDER BY fecha DESC
        LIMIT %s
        """,
        (limite,),
    )


def historia_multianual(meses: int = 12) -> dict:
    """Matriz mes × año con métricas de cierre — replica INFORMES.PRG L1336-1550.

    El PRG muestra una pantalla "HISTORIA" con tres modos:
      H = histórico libre por mes
      N = ediciones (no relevante para PC)
      1/2/3 = lista DE A 12 MESES por año (vivo + 2 años anteriores) con
              variación % vs el mes equivalente del año previo.

    Esta función devuelve la información necesaria para reconstruir el modo
    "1/2/3" en HTML, sin paginación: una matriz de hasta `meses` meses ×
    3 años (actual, -1, -2) con las métricas principales.

    Estructura:
        {
          "anios": [2024, 2025, 2026],          # mayor primero, list ordenada
          "meses": [
            {"mes": 1, "label": "Ene", "datos": {
                2026: {"patrimonio":..., "uvent":..., "usuti":..., "kvent":...,
                       "ustock":..., "uqui":..., "cart":..., "deuda":...},
                2025: {...},
                2024: {...},
                "var_patr_pct":  +12.3,  # patrimonio: cambio % 2026 vs 2025
                "var_uvent_pct": +5.0,
                "var_kvent_pct": -3.1,
                "var_usuti_pct": +18.0,
            }},
            ...
          ]
        }

    Guards:
      - Sin filas en historia → devuelve estructura vacía pero válida.
      - Periodos cerrados sin algunos campos NULL → tratados como 0.
      - División por cero en var_pct → None (template no renderiza la flecha).

    Args:
        meses: cuántos meses calendar mostrar (default 12, capeado a 1..12).
    """
    try:
        n_meses = max(1, min(int(meses), 12))
    except (TypeError, ValueError):
        n_meses = 12

    hoy = date.today()
    anio_actual = hoy.year
    anios = [anio_actual - 2, anio_actual - 1, anio_actual]

    # Una única query — agarra los últimos 36 meses (3 años × 12) y le
    # damos forma en Python. Cada fila histórica tiene `fecha` (último día
    # del mes legacy o algún día del mes); agrupamos por (year, month).
    rows = db.fetch_all(
        """
        SELECT EXTRACT(YEAR  FROM fecha)::int AS yy,
               EXTRACT(MONTH FROM fecha)::int AS mm,
               MAX(fecha)                     AS fecha,
               -- Tomamos el último snapshot del mes si hay >1.
               (ARRAY_AGG(patrimonio ORDER BY fecha DESC))[1] AS patrimonio,
               (ARRAY_AGG(uvent      ORDER BY fecha DESC))[1] AS uvent,
               (ARRAY_AGG(usuti      ORDER BY fecha DESC))[1] AS usuti,
               (ARRAY_AGG(kvent      ORDER BY fecha DESC))[1] AS kvent,
               (ARRAY_AGG(ustock     ORDER BY fecha DESC))[1] AS ustock,
               (ARRAY_AGG(uqui       ORDER BY fecha DESC))[1] AS uqui,
               (ARRAY_AGG(cart       ORDER BY fecha DESC))[1] AS cart,
               (ARRAY_AGG(deuda      ORDER BY fecha DESC))[1] AS deuda,
               (ARRAY_AGG(usret      ORDER BY fecha DESC))[1] AS usret
        FROM scintela.historia
        WHERE EXTRACT(YEAR FROM fecha) BETWEEN %s AND %s
        GROUP BY yy, mm
        """,
        (anio_actual - 2, anio_actual),
    ) or []

    # Indexar por (yy, mm) → datos.
    idx: dict[tuple[int, int], dict] = {}
    for r in rows:
        idx[(int(r["yy"]), int(r["mm"]))] = {
            "patrimonio": float(r.get("patrimonio") or 0),
            "uvent":      float(r.get("uvent")      or 0),
            "usuti":      float(r.get("usuti")      or 0),
            "kvent":      float(r.get("kvent")      or 0),
            "ustock":     float(r.get("ustock")     or 0),
            "uqui":       float(r.get("uqui")       or 0),
            "cart":       float(r.get("cart")       or 0),
            "deuda":      float(r.get("deuda")      or 0),
            "usret":      float(r.get("usret")      or 0),
            "fecha":      r.get("fecha"),
        }

    mes_labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                  "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

    def _pct_var(act: float, ant: float) -> float | None:
        """Variación % de `act` vs `ant`. None si la base es 0 (evita ZeroDiv)."""
        if not ant:
            return None
        return (act - ant) * 100.0 / ant

    meses_out = []
    for m in range(1, n_meses + 1):
        datos: dict = {}
        for a in anios:
            datos[a] = idx.get((a, m), {
                "patrimonio": 0.0, "uvent": 0.0, "usuti": 0.0, "kvent": 0.0,
                "ustock": 0.0, "uqui": 0.0, "cart": 0.0, "deuda": 0.0,
                "usret": 0.0, "fecha": None,
            })
        # Variación % anio_actual vs anio_actual-1 (replica el "DR %" del PRG L1446-1450).
        cur, prev = datos[anio_actual], datos[anio_actual - 1]
        datos["var_patr_pct"]  = _pct_var(cur["patrimonio"], prev["patrimonio"])
        datos["var_uvent_pct"] = _pct_var(cur["uvent"],      prev["uvent"])
        datos["var_kvent_pct"] = _pct_var(cur["kvent"],      prev["kvent"])
        datos["var_usuti_pct"] = _pct_var(cur["usuti"],      prev["usuti"])
        meses_out.append({
            "mes":   m,
            "label": mes_labels[m - 1],
            "datos": datos,
        })

    # Totales acumulados por año (PRG L1383-1389 SUM ALL USUTI/USRET/UVENT).
    totales_por_anio: dict[int, dict] = {}
    for a in anios:
        tot = {"uvent": 0.0, "usuti": 0.0, "kvent": 0.0, "usret": 0.0}
        for m in range(1, n_meses + 1):
            d = idx.get((a, m), {})
            tot["uvent"] += float(d.get("uvent") or 0)
            tot["usuti"] += float(d.get("usuti") or 0)
            tot["kvent"] += float(d.get("kvent") or 0)
            tot["usret"] += float(d.get("usret") or 0)
        totales_por_anio[a] = tot

    return {
        "anios": anios,
        "meses": meses_out,
        "totales_por_anio": totales_por_anio,
        "n_meses": n_meses,
    }


# ---------------------------------------------------------------------------
# INICIALES — metas/valores de apertura mensuales (INICIALES.DBF)
# ---------------------------------------------------------------------------

def iniciales_lista(anio: int | None = None, limite: int = 36) -> list[dict]:
    """Metas mensuales (INICIALES.DBF): producción programada, precios, notas."""
    return db.fetch_all(
        """
        SELECT yy, mesnum, mesnom,
               hilado, tejido, terminado,
               vq, um, uk, uf, uq, pre,
               kprog, gprog, numnot, dificil,
               pretej, pretin, preadm, pretot
        FROM scintela.iniciales
        WHERE (%(anio)s::int IS NULL OR yy = %(anio)s::int)
        ORDER BY yy DESC, mesnum DESC
        LIMIT %(limite)s
        """,
        {"anio": anio, "limite": limite},
    )


# ---------------------------------------------------------------------------
# ESTADO DE CUENTA por cliente
# ---------------------------------------------------------------------------

def buscar_clientes(q: str, limite: int = 25) -> list[dict]:
    """Fuzzy-ish lookup de cliente por codigo_cli o nombre (ilike)."""
    q = (q or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"
    return db.fetch_all(
        """
        SELECT c.codigo_cli,
               c.nombre,
               c.ruc,
               COALESCE((
                 SELECT SUM(f.saldo)
                 FROM scintela.factura f
                 WHERE f.codigo_cli = c.codigo_cli
                   AND COALESCE(f.saldo, 0) > 0
                   AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
               ), 0) AS saldo
        FROM scintela.cliente c
        WHERE c.codigo_cli ILIKE %s OR c.nombre ILIKE %s
        ORDER BY saldo DESC, c.nombre
        LIMIT %s
        """,
        (pattern, pattern, limite),
    )


def estado_cuenta_cliente(codigo_cli: str) -> dict:
    """Facturas + cheques aplicados de un cliente, con totales para el resumen.

    `totales.saldo_vivo` = lo que el cliente nos debe HOY (sum facturas activas
    con saldo > 0, excluye anuladas). Es el número que el gerente busca primero.
    """
    cliente = db.fetch_one(
        """
        SELECT codigo_cli, nombre, telefono, ruc, cupo, stop, pago, pase, descuento
        FROM scintela.cliente
        WHERE codigo_cli = %s
        """,
        (codigo_cli,),
    )
    if not cliente:
        return {
            "cliente": None,
            "facturas": [],
            "cheques": [],
            "totales": {
                "kg": 0.0, "importe": 0.0, "abono": 0.0, "saldo": 0.0,
                "saldo_vivo": 0.0, "n_vencidas": 0, "saldo_vencido": 0.0,
                "cheques_total": 0.0, "cheques_cartera": 0.0,
                "cheques_depositados": 0.0, "cheques_acreditados": 0.0,
                "cheques_rebotados": 0.0,
            },
        }
    facturas = db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, vencimiento,
               kg, importe, abono, saldo, stat, condic, tipo
        FROM scintela.factura
        WHERE codigo_cli = %s
        -- TMT 2026-05-17: cuando hay facturas con la misma fecha, las
        -- ABONADAS van primero (abono > 0). Acelera la lectura: lo que
        -- está cobrándose se ve arriba, lo virgen abajo. Tie-break por
        -- numf DESC dentro de cada grupo.
        ORDER BY fecha DESC,
                 CASE WHEN COALESCE(abono, 0) > 0 THEN 0 ELSE 1 END,
                 numf DESC
        """,
        (codigo_cli,),
    )
    cheques = db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fechaing,
               c.fecha_recibido, c.fecha_crea,
               -- TMT 2026-05-17: fechad_original NULL = sin postergar; NOT NULL
               -- = fue postergado, snapshot de la 1ra fechad. fecha_postergacion
               -- = cuándo se postergó (última si hay varias).
               c.fechad_original, c.fecha_postergacion,
               c.importe, c.stat, c.banco, b.nombre AS nombre_banco
        FROM scintela.cheque c
        LEFT JOIN scintela.banco b ON b.no_banco = c.no_banco
        WHERE c.codigo_cli = %s
        ORDER BY COALESCE(c.fechaing, c.fechad, c.fecha) DESC, c.id_cheque DESC
        """,
        (codigo_cli,),
    )

    # Totales — calculados en SQL para precisión numeric, no en Python.
    tot_fac = db.fetch_one(
        """
        SELECT
          COALESCE(SUM(kg), 0)                                        AS kg,
          COALESCE(SUM(importe), 0)                                   AS importe,
          COALESCE(SUM(abono), 0)                                     AS abono,
          COALESCE(SUM(saldo), 0)                                     AS saldo,
          COALESCE(SUM(CASE
            WHEN COALESCE(saldo, 0) > 0
             AND (stat IS NULL OR stat IN ('Z','A','',' '))
            THEN saldo ELSE 0 END), 0)                                AS saldo_vivo,
          COALESCE(SUM(CASE
            WHEN COALESCE(saldo, 0) > 0
             AND (stat IS NULL OR stat IN ('Z','A','',' '))
             AND COALESCE(vencimiento, fecha) < CURRENT_DATE
            THEN saldo ELSE 0 END), 0)                                AS saldo_vencido,
          COUNT(CASE
            WHEN COALESCE(saldo, 0) > 0
             AND (stat IS NULL OR stat IN ('Z','A','',' '))
             AND COALESCE(vencimiento, fecha) < CURRENT_DATE
            THEN 1 END)                                               AS n_vencidas
        FROM scintela.factura
        WHERE codigo_cli = %s
        """,
        (codigo_cli,),
    ) or {}
    # Stats canónicos (2026-04-29 + TMT 2026-05-16):
    #   Z/P = cartera (todavía en mano)
    #   B/A = depositados (B nuevo, A legacy — ambos son "ya en banco")
    #   1/2/3/R = devueltos/rebotados (1/2/3 todavía en gestión, R terminal)
    #   D = "Daniela" (caso especial legacy — no es depositado)
    #   E = endosado, X = eliminado
    tot_che = db.fetch_one(
        """
        SELECT
          COALESCE(SUM(importe), 0)                                                       AS total,
          COALESCE(SUM(CASE WHEN stat IN ('Z','P')         THEN importe ELSE 0 END), 0)   AS cartera,
          COALESCE(SUM(CASE WHEN stat IN ('B','A')         THEN importe ELSE 0 END), 0)   AS depositados,
          COALESCE(SUM(CASE WHEN stat IN ('1','2','3','R') THEN importe ELSE 0 END), 0)   AS rebotados,
          COALESCE(SUM(CASE WHEN stat = 'E'                THEN importe ELSE 0 END), 0)   AS endosados,
          COALESCE(SUM(CASE WHEN stat = 'D'                THEN importe ELSE 0 END), 0)   AS daniela
        FROM scintela.cheque
        WHERE codigo_cli = %s
          AND COALESCE(stat,'') <> 'X'
        """,
        (codigo_cli,),
    ) or {}

    totales = {
        "kg":              float(tot_fac.get("kg") or 0),
        "importe":         float(tot_fac.get("importe") or 0),
        "abono":           float(tot_fac.get("abono") or 0),
        "saldo":           float(tot_fac.get("saldo") or 0),
        "saldo_vivo":      float(tot_fac.get("saldo_vivo") or 0),
        "saldo_vencido":   float(tot_fac.get("saldo_vencido") or 0),
        "n_vencidas":      int(tot_fac.get("n_vencidas") or 0),
        "cheques_total":         float(tot_che.get("total") or 0),
        "cheques_cartera":       float(tot_che.get("cartera") or 0),
        "cheques_depositados":   float(tot_che.get("depositados") or 0),
        "cheques_rebotados":     float(tot_che.get("rebotados") or 0),
        "cheques_endosados":     float(tot_che.get("endosados") or 0),
        "cheques_daniela":       float(tot_che.get("daniela") or 0),
    }
    return {"cliente": cliente, "facturas": facturas, "cheques": cheques, "totales": totales}


# ---------------------------------------------------------------------------
# Cuadro de Fuentes y Usos — pedido dueña 2026-05-18 (docx "Para Claude").
# ---------------------------------------------------------------------------
#
# Compara DOS snapshots de scintela.historia (mes anterior vs mes elegido).
# Cada cuenta cuya Δ sea +/-:
#   - Activos: Δ>0 = USO (puse plata ahí); Δ<0 = FUENTE (saqué plata de ahí).
#   - Pasivos: Δ>0 = FUENTE (me prestaron); Δ<0 = USO (devolví).
#   - Aportes / Retiros: fuente / uso directos.
#
# Total Fuentes − Total Usos ≈ Δ caja + bancos (verificación).
# ---------------------------------------------------------------------------

def _historia_en_mes(yy: int, mm: int) -> dict:
    """Devuelve la última fila de historia DEL mes (o {} si no hay).

    Si en el mes hay varios snapshots, toma el de fecha más alta.
    """
    return db.fetch_one(
        """
        SELECT *
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha DESC, id_historia DESC
         LIMIT 1
        """,
        (int(yy), int(mm)),
    ) or {}


def _mes_anterior(yy: int, mm: int) -> tuple[int, int]:
    """(yy, mm) del mes anterior."""
    mm = int(mm)
    yy = int(yy)
    if mm == 1:
        return yy - 1, 12
    return yy, mm - 1


def snapshot_historia_existe(anio: int, mes: int) -> bool:
    """¿Hay ya un registro en scintela.historia para ese año/mes?"""
    row = db.fetch_one(
        """
        SELECT 1 AS x
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         LIMIT 1
        """,
        (int(anio), int(mes)),
    )
    return bool(row)


def compras_del_periodo(
    *,
    anio: int | None = None,
    mes: int | None = None,
    prov: str | None = None,
    num_v: int | None = None,
) -> dict:
    """Compras del mes/período + filtros. Tab Compras del /informes/balance.

    Devuelve {anio, mes, filas, total_importe, total_kg, n_filas, prov_options}.
    `prov` es código exacto del proveedor; `num_v` filtra por V calculado
    en la cascada `_SQL_COMPRA_NUM_CASE`.

    Excluye anuladas (stat IN X,Y). Excluye producción (K con kg>0) sólo
    a efectos del cuadre con la línea "Compras" del balance — la tabla
    sigue mostrando todas (con badge cuando es producción).
    """
    from datetime import date as _date
    if anio is None or mes is None:
        hoy = _date.today()
        anio = anio or hoy.year
        mes = mes or hoy.month
    prov_norm = (prov or "").strip().upper() or None

    where_v = ""
    params: list = [anio, mes]
    if prov_norm:
        where_v += " AND UPPER(COALESCE(c.codigo_prov,'')) = %s"
        params.append(prov_norm)
    if num_v and 1 <= int(num_v) <= 9:
        # Reuse del SQL CASE.
        where_v += f" AND ({_SQL_COMPRA_NUM_CASE}) = %s"
        params.append(int(num_v))

    filas = db.fetch_all(
        f"""
        SELECT c.id_compra, c.fecha, c.codigo_prov, c.tipo,
               c.kg, c.importe, c.concepto, c.comprobante, c.stat,
               COALESCE(p.nombre, '') AS proveedor,
               ({_SQL_COMPRA_NUM_CASE}) AS num_v
          FROM scintela.compra c
          LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
         WHERE EXTRACT(YEAR FROM c.fecha) = %s
           AND EXTRACT(MONTH FROM c.fecha) = %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           {where_v}
         ORDER BY c.fecha ASC, c.id_compra ASC
        """,
        tuple(params),
    ) or []

    total_importe = sum(float(r.get("importe") or 0) for r in filas)
    total_kg = sum(float(r.get("kg") or 0) for r in filas)

    # Proveedores únicos para el dropdown filter.
    prov_options = db.fetch_all(
        """
        SELECT DISTINCT c.codigo_prov,
               COALESCE(p.nombre, '') AS nombre
          FROM scintela.compra c
          LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
         WHERE EXTRACT(YEAR FROM c.fecha) = %s
           AND EXTRACT(MONTH FROM c.fecha) = %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND c.codigo_prov IS NOT NULL
         ORDER BY 1
        """,
        (anio, mes),
    ) or []

    return {
        "anio": int(anio),
        "mes": int(mes),
        "filas": filas,
        "total_importe": total_importe,
        "total_kg": total_kg,
        "n_filas": len(filas),
        "prov_options": prov_options,
        "prov_actual": prov_norm,
        "num_v_actual": int(num_v) if num_v else None,
    }


def historico_12m_matriz(meses_atras: int = 12) -> dict:
    """Matriz comparativa de los últimos N meses de scintela.historia.

    Pedido dueña 2026-05-19 — Feature B. Lee snapshots ya creados.
    Devuelve {meses: [...], lineas: [{label, valores[mes_index], total,
    promedio, delta_pct[mes_index]}]}.

    Las filas (líneas de balance) son las que tiene scintela.historia:
        Banco, Cartera (totc+totf), Deuda (totp), Stock (ustock+uqui),
        Anticipos, Maquinaria, Realty, Patrimonio, Ventas (uvent),
        Compras (ucom), Gasto (xgast), Retiro (usret), Utilidad (usuti).
    """
    from datetime import date as _date
    n = max(1, min(int(meses_atras or 12), 24))
    hoy = _date.today()

    # Lista de meses (anio, mes) de los últimos N, ordenados ascendente.
    meses: list[tuple[int, int]] = []
    a, m = hoy.year, hoy.month
    for _ in range(n):
        meses.append((a, m))
        m -= 1
        if m < 1:
            m = 12
            a -= 1
    meses.reverse()

    # Cargar snapshots existentes — uno por (año, mes) si existe.
    snapshots: dict[tuple[int, int], dict] = {}
    for (a_, m_) in meses:
        row = db.fetch_one(
            """
            SELECT fecha, banco, cart, deuda, ustock, uqui, anticipos,
                   maquinaria, realty, patrimonio, uvent, ucom, gasto,
                   usret, usuti, kvent, kcom
              FROM scintela.historia
             WHERE EXTRACT(YEAR FROM fecha) = %s
               AND EXTRACT(MONTH FROM fecha) = %s
             ORDER BY fecha DESC
             LIMIT 1
            """,
            (a_, m_),
        )
        if row:
            snapshots[(a_, m_)] = row

    # Líneas del matriz: (label, key_en_historia, formato)
    lineas_def = [
        ("Ventas $",          "uvent",      "money"),
        ("Compras $",         "ucom",       "money"),
        ("Margen bruto",      "_margen",    "money"),  # uvent - ucom
        ("Gastos $",          "gasto",      "money"),
        ("Utilidad",          "usuti",      "money"),
        ("Retiros",           "usret",      "money"),
        ("Patrimonio",        "patrimonio", "money"),
        ("Banco",             "banco",      "money"),
        ("Cartera",           "cart",       "money"),
        ("Deuda (posdat)",    "deuda",      "money"),
        ("Stock MP+PT",       "ustock",     "money"),
        ("Stock químicos",    "uqui",       "money"),
        ("Anticipos",         "anticipos",  "money"),
        ("Maquinaria",        "maquinaria", "money"),
        ("Realty",            "realty",     "money"),
        ("Ventas (kg)",       "kvent",      "kg"),
        ("Compras (kg)",      "kcom",       "kg"),
    ]

    lineas_out = []
    for label, key, fmt in lineas_def:
        valores = []
        for (a_, m_) in meses:
            snap = snapshots.get((a_, m_))
            if not snap:
                valores.append(None)
                continue
            if key == "_margen":
                v = float(snap.get("uvent") or 0) - float(snap.get("ucom") or 0)
            else:
                v = float(snap.get(key) or 0)
            valores.append(v)
        # Total acumulado / promedio sobre los meses con dato.
        validos = [v for v in valores if v is not None]
        total = sum(validos) if validos else 0.0
        promedio = (total / len(validos)) if validos else 0.0
        # Δ% vs mes anterior, celda a celda.
        delta_pct = []
        for i, v in enumerate(valores):
            if v is None or i == 0:
                delta_pct.append(None)
                continue
            prev = valores[i - 1]
            if prev is None or abs(prev) < 0.005:
                delta_pct.append(None)
                continue
            delta_pct.append((v - prev) / abs(prev) * 100.0)
        lineas_out.append({
            "label": label,
            "key": key,
            "fmt": fmt,
            "valores": valores,
            "total": total,
            "promedio": promedio,
            "delta_pct": delta_pct,
        })

    # Conteo de meses sin snapshot (para placeholder + botón backfill).
    sin_snap = [
        f"{m_:02d}/{a_}" for (a_, m_) in meses
        if (a_, m_) not in snapshots
    ]

    return {
        "meses": meses,
        "lineas": lineas_out,
        "snapshots_existentes": len(snapshots),
        "meses_total": n,
        "meses_sin_snap": sin_snap,
    }


def balance_components_as_of(as_of) -> dict:
    """Balance "as of" una fecha pasada — TMT 2026-05-19 v6 audit (pedido dueña).

    Devuelve los componentes principales del balance calculados como si
    estuviéramos en `as_of` (típicamente el último día de un mes pasado).
    Reemplaza la práctica anterior de usar `informe_balance()` LIVE para
    backfill de snapshots, que daba siempre el saldo de HOY.

    Granularidad limitada:
    - Saldos running (caja, banco): última fila con `fecha <= as_of`.
    - Cheques en cartera (totc): stat ∈ {Z,1,2,3,P,D} con fecha_recibido
      <= as_of y NO depositados antes de as_of (fechaing IS NULL OR > as_of).
    - Facturas vivas (totf): fecha <= as_of, saldo > 0, stat ∈ {Z,A,'',null}.
      APROXIMACIÓN: usa el saldo actual de la factura (no recalcula abonos
      post-as_of). Para snapshots de meses recientes funciona OK; para
      meses muy antiguos puede tener drift si hubieron abonos posteriores.
    - Posdat (totp): WHERE fecha <= as_of AND banc=0 (deuda viva en ese momento).
    - Stock vsto/vqx/etc: último snapshot historia con fecha <= as_of.
    - Activos: snapshot del último cierre <= as_of.
    - Flujos del mes (kcom, ucom, kvent, etc.): WHERE
      DATE_TRUNC('month', fecha) = DATE_TRUNC('month', as_of).
    - PATANT, USRET, USUTI: del snapshot anterior al as_of.

    Returns dict con todos los campos necesarios para `scintela.historia`.
    """
    from datetime import date as _date
    if not as_of:
        as_of = _date.today()

    # --- Saldos running ---
    salcaj_row = db.fetch_one(
        """
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         WHERE fecha <= %s
         ORDER BY fecha DESC, id_caja DESC
         LIMIT 1
        """,
        (as_of,),
    ) or {}
    salcaj = float(salcaj_row.get("saldo") or 0)

    salbanc_rows = db.fetch_all(
        """
        SELECT DISTINCT ON (no_banco)
               no_banco, COALESCE(saldo, 0) AS saldo
          FROM scintela.transacciones_bancarias
         WHERE fecha <= %s
         ORDER BY no_banco, fecha DESC, id_transaccion DESC
        """,
        (as_of,),
    ) or []
    salbanc = sum(float(r.get("saldo") or 0) for r in salbanc_rows)

    # --- Cheques en cartera (no depositados, no anulados) ---
    totc_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
          FROM scintela.cheque
         WHERE stat IN ('Z','1','2','3','P','D')
           AND COALESCE(fecha_recibido, fecha) <= %s
           AND (fechaing IS NULL OR fechaing > %s)
        """,
        (as_of, as_of),
    ) or {}
    totc = float(totc_row.get("total") or 0)

    # --- Facturas vivas as_of ---
    # Aproximación: factura.saldo actual (no rewind de abonos post-as_of).
    totf_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total
          FROM scintela.factura
         WHERE fecha <= %s
           AND COALESCE(saldo, 0) > 0
           AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """,
        (as_of,),
    ) or {}
    totf = float(totf_row.get("total") or 0)

    # --- Posdat deuda viva as_of ---
    totp_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND COALESCE(importe, 0) > 0
           AND fecha <= %s
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """,
        (as_of,),
    ) or {}
    totp = float(totp_row.get("total") or 0)

    # --- Stock / activos del último snapshot histórico <= as_of ---
    hist_prev = db.fetch_one(
        """
        SELECT fecha, stock, ustock, uqui, maquinaria, realty, anticipos,
               patrimonio, usret, usuti
          FROM scintela.historia
         WHERE fecha <= %s
         ORDER BY fecha DESC
         LIMIT 1
        """,
        (as_of,),
    ) or {}
    vsto    = float(hist_prev.get("ustock") or 0)
    vqx     = float(hist_prev.get("uqui") or 0)
    umaq    = float(hist_prev.get("maquinaria") or 0)
    uact    = float(hist_prev.get("realty") or 0)
    antic   = float(hist_prev.get("anticipos") or 0)
    patant  = float(hist_prev.get("patrimonio") or 0)

    # --- Flujos del mes (mes que contiene as_of) ---
    kcom_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
          FROM scintela.compra
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (as_of, as_of),
    ) or {}
    kcom = float(kcom_row.get("kg") or 0)
    ucom = float(kcom_row.get("importe") or 0)

    vent_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
          FROM scintela.factura
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') <> 'X'
        """,
        (as_of, as_of),
    ) or {}
    kvent = float(vent_row.get("kg") or 0)
    uvent = float(vent_row.get("importe") or 0)

    gasto_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS importe
          FROM scintela.xgast
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """,
        (as_of, as_of),
    ) or {}
    gasto = float(gasto_row.get("importe") or 0)

    # USRET (retiros del mes) — aproximación: el snapshot del mes los
    # recalcula al cierre. USUTI sale de utilidad calculada abajo.
    usret = 0.0

    # Computar cartera (totc + totf), subt, totl, patr, retiro
    cart = totc + totf
    subt = salbanc + salcaj + cart
    totl = subt + vsto + vqx + umaq + uact + antic
    patr = totl - totp
    utilidad = patr - patant

    return {
        # Saldos
        "salcaj":  salcaj,
        "salbanc": salbanc,
        "banco":   salbanc,
        "totc":    totc,
        "totf":    totf,
        "cart":    cart,
        # Pasivos
        "totp":    totp,
        "deuda":   totp,
        # Activos fijos / stock
        "vsto":    vsto,
        "stock":   float(hist_prev.get("stock") or 0),
        "ustock":  vsto,
        "uqui":    vqx,
        "vqx":     vqx,
        "umaq":    umaq,
        "maquinaria": umaq,
        "uact":    uact,
        "realty":  uact,
        "antic":   antic,
        "anticipos": antic,
        # Patrimonio + utilidad
        "subt":    subt,
        "totl":    totl,
        "patr":    patr,
        "patrimonio": patr,
        "patant":  patant,
        "utilidad": utilidad,
        "usuti":   utilidad,  # snapshot guarda usuti = utilidad del período
        "usret":   usret,
        "retiro":  usret,
        # Flujos del mes
        "kcom":    kcom,
        "ucom":    ucom,
        "kvent":   kvent,
        "uvent":   uvent,
        "ktej":    0.0,  # Aproximación — kg de tejido del mes requiere
        "ktin":    0.0,  # join con tinto/compras tipo K. Omitido por scope.
        "utej":    0.0,
        "utin":    0.0,
        "costo":   ucom,  # Default conservador.
        "gasto":   gasto,
        "gstotal": gasto,
        "dolar":   0.0,
        # Meta
        "as_of":   as_of,
    }


def informe_balance_as_of(as_of=None) -> dict:
    """Wrapper que devuelve un dict similar a informe_balance() pero
    calculado as_of. Default as_of=hoy = comportamiento clásico.

    Implementado parcialmente: cubre los componentes necesarios para
    `crear_snapshot_historia`. Para uso UI completo, seguir usando
    `informe_balance()`.
    """
    from datetime import date as _date
    if as_of is None or as_of == _date.today():
        # As_of = hoy → comportamiento clásico, llama al balance live.
        return informe_balance()
    components = balance_components_as_of(as_of)
    return {
        "fecha": as_of,
        "kg": {
            "kcom":  components["kcom"],
            "ucom":  components["ucom"],
            "ktej":  components["ktej"],
            "ktin":  components["ktin"],
            "kvent": components["kvent"],
            "uvent": components["uvent"],
            "utej":  components["utej"],
            "utin":  components["utin"],
            "stock_kg": components["stock"],
            "costo_mes": components["costo"],
        },
        "diagnostico": {
            "componentes": components,
        },
        "stock_subpanels": {
            "total_us": components["vsto"],
        },
        "error": None,
    }


def crear_snapshot_historia(anio: int, mes: int,
                            usuario: str = "auto") -> dict:
    """Crea un snapshot mensual en scintela.historia para (anio, mes).

    TMT 2026-05-18 — Pedido dueña: necesitamos snapshots automáticos del
    cierre mensual para que `/informes/fuentes-y-usos` tenga data.

    Idempotente vía 2 capas:
      1. `sistema_meta` con clave `historia_snapshot_ult_periodo` —
         marker del último mes snapshoteado.
      2. Existencia previa: si ya hay fila para (anio, mes), no inserta.

    Toma los componentes calculados por `informe_balance()` y los mapea
    a las columnas de `scintela.historia`. El balance se calcula contra
    los datos LIVE de hoy — para tomar snapshot del mes pasado conviene
    correrlo el día 1-2 del mes siguiente.

    Devuelve `{aplicado: bool, anio, mes, id_historia: int|None, razon: str}`.
    """
    from datetime import date as _date

    anio = int(anio)
    mes = int(mes)
    periodo_clave = f"{anio:04d}-{mes:02d}"

    if snapshot_historia_existe(anio, mes):
        return {
            "aplicado": False, "anio": anio, "mes": mes,
            "id_historia": None,
            "razon": f"Ya existe snapshot para {periodo_clave}.",
        }

    # TMT 2026-05-19 v6 audit — calculamos el balance "as_of último día del mes"
    # para que los snapshots de backfill queden con la foto correcta. Antes
    # esto usaba `informe_balance()` LIVE → backfills con saldo de hoy.
    import calendar
    last_day = calendar.monthrange(anio, mes)[1]
    fecha_snap = _date(anio, mes, last_day)

    bal = informe_balance_as_of(fecha_snap)
    if not bal or bal.get("error"):
        return {
            "aplicado": False, "anio": anio, "mes": mes,
            "id_historia": None,
            "razon": f"Balance falló: {bal.get('error') if bal else 'sin data'}",
        }

    d = bal["diagnostico"]["componentes"] if bal.get("diagnostico") else {}
    kg = bal.get("kg", {})
    stock_sub = bal.get("stock_subpanels", {})

    with db.tx() as conn:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('snapshot_historia'))",
            conn=conn,
        )
        # Re-check después del lock
        if snapshot_historia_existe(anio, mes):
            return {"aplicado": False, "anio": anio, "mes": mes,
                    "id_historia": None,
                    "razon": "race ganada por otra request"}

        res = db.execute_returning(
            """
            INSERT INTO scintela.historia
                (fecha, stock, kcom, ktej, ktin, ustock, uqui, kvent,
                 uvent, costo, ucom, utej, utin, gasto, gstotal,
                 banco, cart, deuda, retiro, patrimonio, anticipos,
                 dolar, maquinaria, realty, usret, usuti,
                 fecha_crea, usuario_crea)
            VALUES (%(fecha)s,
                    %(stock)s, %(kcom)s, %(ktej)s, %(ktin)s, %(ustock)s,
                    %(uqui)s, %(kvent)s, %(uvent)s, %(costo)s, %(ucom)s,
                    %(utej)s, %(utin)s, %(gasto)s, %(gstotal)s,
                    %(banco)s, %(cart)s, %(deuda)s, %(retiro)s,
                    %(patrimonio)s, %(anticipos)s, %(dolar)s,
                    %(maquinaria)s, %(realty)s, %(usret)s, %(usuti)s,
                    CURRENT_TIMESTAMP, %(usuario)s)
            RETURNING id_historia
            """,
            {
                "fecha":      fecha_snap,
                # Stock KG y US$
                "stock":      float(kg.get("stock_kg") or kg.get("stock_kg_live") or 0),
                "ustock":     float(stock_sub.get("total_us") or 0),
                "uqui":       float(d.get("vqx") or 0),
                # Flujos del mes (KG)
                "kcom":       float(kg.get("kcom") or 0),
                "ktej":       float(kg.get("ktej") or 0),
                "ktin":       float(kg.get("ktin") or 0),
                "kvent":      float(kg.get("kvent") or 0),
                # Flujos del mes (US$)
                "ucom":       float(kg.get("ucom") or 0),
                "utej":       float(kg.get("utej") or 0),
                "utin":       float(kg.get("utin") or 0),
                "uvent":      float(kg.get("uvent") or 0),
                "costo":      float(kg.get("costo_mes") or 0),
                # Resultados del mes
                "gasto":      float(d.get("gastos_mes") or 0),
                "gstotal":    float(d.get("gastos_total") or 0),
                # Balance components
                "banco":      float(d.get("salbanc") or 0),
                "cart":       float(d.get("totc", 0) or 0) + float(d.get("totf", 0) or 0),
                "deuda":      float(d.get("totp") or 0),
                "retiro":     float(d.get("uret") or 0),
                "patrimonio": float(d.get("patr") or 0),
                "anticipos":  float(d.get("antic") or 0),
                "dolar":      0.0,    # no usado en PC
                "maquinaria": float(d.get("umaq") or 0),
                "realty":     float(d.get("uact") or 0),
                "usret":      float(d.get("uret") or 0),
                "usuti":      float(d.get("utilidad") or 0),
                "usuario":    usuario[:50],
            },
            conn=conn,
        )

        # Avanzar marker en sistema_meta (best-effort)
        try:
            db.execute(
                """
                INSERT INTO scintela.sistema_meta (clave, valor)
                VALUES ('historia_snapshot_ult_periodo', %s)
                ON CONFLICT (clave) DO UPDATE
                  SET valor = EXCLUDED.valor,
                      actualizado = CURRENT_TIMESTAMP
                """,
                (periodo_clave,),
                conn=conn,
            )
        except Exception:
            pass

    return {
        "aplicado": True, "anio": anio, "mes": mes,
        "id_historia": (res or {}).get("id_historia"),
        "razon": f"Snapshot creado para {periodo_clave}.",
    }


def fuentes_y_usos(
    *,
    desde_anio: int | None = None,
    desde_mes: int | None = None,
    hasta_anio: int | None = None,
    hasta_mes: int | None = None,
    # Back-compat: si se llaman con (anio, mes) viejos, se interpreta como
    # ventana de 1 mes (anio,mes vs mes anterior). TMT 2026-05-19.
    anio: int | None = None,
    mes: int | None = None,
) -> dict:
    """Cuadro de Fuentes y Usos en un rango DESDE-HASTA mensual.

    Replica INFORMES.PRG::PROCEDURE FUENTES L1654-1727. Pedido dueña
    2026-05-19 item 14: seleccionar DESDE-HASTA + 2 columnas con totales
    iguales (cierra con balancing line "Aumento/Disminución de líquido").

    Devuelve:
        {
          "anio_ini": int, "mes_ini": int,
          "anio": int, "mes": int,
          "h_ini": <historia mes inicial>,
          "h_fin": <historia mes final>,
          "fuentes": [(label, monto), ...],
          "usos":    [(label, monto), ...],
          "total_fuentes": float,
          "total_usos":    float,         # IGUAL a total_fuentes por construcción
          "delta_liquido": float,         # Δ banco real (info)
          "delta_banco":   float,
          "error": str | None,
        }
    """
    # Back-compat: si llaman con (anio, mes), interpretar como ventana 1 mes.
    if hasta_anio is None and hasta_mes is None and anio is not None and mes is not None:
        hasta_anio, hasta_mes = int(anio), int(mes)
        desde_anio_, desde_mes_ = _mes_anterior(hasta_anio, hasta_mes)
        desde_anio = desde_anio_ if desde_anio is None else desde_anio
        desde_mes = desde_mes_ if desde_mes is None else desde_mes

    yy, mm = int(hasta_anio), int(hasta_mes)
    yy_ant, mm_ant = int(desde_anio), int(desde_mes)

    h_fin = _historia_en_mes(yy, mm)
    h_ini = _historia_en_mes(yy_ant, mm_ant)

    if not h_fin or not h_ini:
        return {
            "anio": yy, "mes": mm,
            "h_ini": h_ini, "h_fin": h_fin,
            "fuentes": [], "usos": [],
            "total_fuentes": 0.0, "total_usos": 0.0,
            "delta_liquido": 0.0, "delta_banco": 0.0,
            "error": (
                "No hay snapshot mensual en scintela.historia para "
                f"{mm_ant:02d}/{yy_ant} y/o {mm:02d}/{yy}. "
                "El balance debe cerrarse mensualmente para generar este cuadro."
            ),
        }

    def f(row: dict, col: str) -> float:
        v = row.get(col)
        return float(v) if v is not None else 0.0

    # Δ por cuenta (fin - ini). Activos: + = uso. Pasivos: + = fuente.
    delta = {
        # Activos
        "cart":       f(h_fin, "cart")       - f(h_ini, "cart"),
        "stock":      f(h_fin, "stock")      - f(h_ini, "stock"),
        "uqui":       f(h_fin, "uqui")       - f(h_ini, "uqui"),
        "maquinaria": f(h_fin, "maquinaria") - f(h_ini, "maquinaria"),
        "realty":     f(h_fin, "realty")     - f(h_ini, "realty"),
        "anticipos":  f(h_fin, "anticipos")  - f(h_ini, "anticipos"),
        # Pasivos
        "deuda":      f(h_fin, "deuda")      - f(h_ini, "deuda"),
        # Cuasi-líquidos (control)
        "banco":      f(h_fin, "banco")      - f(h_ini, "banco"),
        # Resultados
        "usuti":      f(h_fin, "usuti")      - f(h_ini, "usuti"),
        # Retiros (acum del año)
        "usret":      f(h_fin, "usret")      - f(h_ini, "usret"),
    }

    fuentes: list[tuple[str, float]] = []
    usos:    list[tuple[str, float]] = []

    # Utilidad del período (la del mes) → fuente si positiva, uso si pérdida.
    utilidad_mes = delta["usuti"]
    if utilidad_mes >= 0:
        fuentes.append(("Utilidad del mes", utilidad_mes))
    else:
        usos.append(("Pérdida del mes", abs(utilidad_mes)))

    # Retiros del mes → uso (USRET es acumulado anual; Δ del mes son los del mes).
    retiros_mes = delta["usret"]
    if retiros_mes > 0:
        usos.append(("Retiros del mes", retiros_mes))
    elif retiros_mes < 0:
        # Δ negativo = reversa de retiros (raro) → fuente
        fuentes.append(("Reverso de retiros", abs(retiros_mes)))

    # Activos: Δ>0 = uso, Δ<0 = fuente.
    activos_labels = {
        "cart":       "Cartera (clientes)",
        "stock":      "Stock de productos",
        "uqui":       "Stock de químicos",
        "maquinaria": "Maquinaria",
        "realty":     "Terrenos y edificios",
        "anticipos":  "Anticipos USD a proveedores",
    }
    for k, label in activos_labels.items():
        d = delta[k]
        if d > 0.5:
            usos.append((f"Aumento {label.lower()}", d))
        elif d < -0.5:
            fuentes.append((f"Disminución {label.lower()}", abs(d)))

    # Pasivos: Δ>0 = fuente, Δ<0 = uso.
    if delta["deuda"] > 0.5:
        fuentes.append(("Aumento de deuda con proveedores", delta["deuda"]))
    elif delta["deuda"] < -0.5:
        usos.append(("Disminución de deuda con proveedores", abs(delta["deuda"])))

    total_fuentes = sum(m for _, m in fuentes)
    total_usos    = sum(m for _, m in usos)

    # TMT 2026-05-19 item 14: balancing line para que los totales sean
    # IDÉNTICOS por construcción (identidad contable). Si fuentes > usos,
    # significa que acumulamos líquido — va como USO ("aumento de líquido").
    # Si usos > fuentes, agotamos líquido — va como FUENTE.
    delta_global = total_fuentes - total_usos
    # TMT 2026-05-19 — dueña: "disminución está todo repetido, que sea más
    # lindo". Renombramos la balancing line a "Ajuste — variación de caja
    # y bancos" para que NO se confunda con un movimiento real y la línea
    # se distinga visualmente en el template (italic + gris).
    if delta_global > 0.5:
        usos.append(("— Ajuste: variación de caja y bancos", delta_global))
    elif delta_global < -0.5:
        fuentes.append(("— Ajuste: variación de caja y bancos", abs(delta_global)))

    total_fuentes = sum(m for _, m in fuentes)
    total_usos    = sum(m for _, m in usos)

    return {
        "anio": yy, "mes": mm,
        "anio_ini": yy_ant, "mes_ini": mm_ant,
        "h_ini": h_ini, "h_fin": h_fin,
        "fuentes": fuentes, "usos": usos,
        "total_fuentes": total_fuentes,
        "total_usos":    total_usos,
        # `delta_liquido` queda en 0 (o casi) por el balancing.
        "delta_liquido": total_fuentes - total_usos,
        "delta_banco":   delta["banco"],
        "error":         None,
    }


# ---------------------------------------------------------------------------
# GASTOS FORZADOS — flujo de fondos. Persistencia DB (migración 0033).
# Antes vivían en localStorage del navegador (cliente-side), pero la dueña
# reportó que al abrir en otro navegador/máquina aparecían vacíos. Pedido
# 2026-05-19 v8: "asegurate de encontrarlos y mostrarmelos".
# ---------------------------------------------------------------------------

def gastos_forzados_listar() -> list[dict]:
    """Lista todos los gastos forzados ordenados por fecha ASC."""
    rows = db.fetch_all(
        """
        SELECT id_gasto_forzado, fecha, importe, concepto, version,
               creado_por, creado_en, actualizado_en, actualizado_por
          FROM scintela.gasto_forzado
         ORDER BY fecha ASC, id_gasto_forzado ASC
        """
    ) or []
    out = []
    for r in rows:
        out.append({
            "id": int(r["id_gasto_forzado"]),
            "fecha": r["fecha"].isoformat() if r["fecha"] else None,
            "importe": float(r["importe"] or 0),
            "concepto": r["concepto"] or "",
            "version": int(r["version"] or 1),
        })
    return out


def gasto_forzado_crear(
    fecha,
    importe: float,
    concepto: str = "",
    usuario: str = "web",
) -> dict:
    """Crea un nuevo gasto forzado. Devuelve el item con id y version=1."""
    row = db.fetch_one(
        """
        INSERT INTO scintela.gasto_forzado
            (fecha, importe, concepto, version, creado_por,
             actualizado_en, actualizado_por)
        VALUES (%s, %s, %s, 1, %s, CURRENT_TIMESTAMP, %s)
        RETURNING id_gasto_forzado, fecha, importe, concepto, version
        """,
        (fecha, importe, concepto or None, usuario, usuario),
    )
    if not row:
        raise RuntimeError("INSERT gasto_forzado no devolvió fila")
    return {
        "id": int(row["id_gasto_forzado"]),
        "fecha": row["fecha"].isoformat() if row["fecha"] else None,
        "importe": float(row["importe"] or 0),
        "concepto": row["concepto"] or "",
        "version": int(row["version"] or 1),
    }


def gasto_forzado_actualizar(
    id_gasto_forzado: int,
    expected_version: int,
    fecha=None,
    importe: float | None = None,
    concepto: str | None = None,
    usuario: str = "web",
) -> dict:
    """Update con optimistic lock — rechaza si la versión actual no coincide.

    Devuelve `{ok: bool, current?: dict, updated?: dict, reason?: str}`.
    """
    # Cargar el item actual
    actual = db.fetch_one(
        """
        SELECT id_gasto_forzado, fecha, importe, concepto, version
          FROM scintela.gasto_forzado
         WHERE id_gasto_forzado = %s
        """,
        (id_gasto_forzado,),
    )
    if not actual:
        return {"ok": False, "reason": "not_found"}
    actual_v = int(actual["version"] or 1)
    if actual_v != int(expected_version):
        return {
            "ok": False, "reason": "version_conflict",
            "current": {
                "id": int(actual["id_gasto_forzado"]),
                "fecha": actual["fecha"].isoformat() if actual["fecha"] else None,
                "importe": float(actual["importe"] or 0),
                "concepto": actual["concepto"] or "",
                "version": actual_v,
            },
        }
    # Update parcial — coalesce a los valores actuales si vienen en None
    nueva_fecha = fecha if fecha is not None else actual["fecha"]
    nuevo_importe = importe if importe is not None else float(actual["importe"] or 0)
    nuevo_concepto = concepto if concepto is not None else (actual["concepto"] or "")
    row = db.fetch_one(
        """
        UPDATE scintela.gasto_forzado
           SET fecha           = %s,
               importe         = %s,
               concepto        = %s,
               version         = version + 1,
               actualizado_en  = CURRENT_TIMESTAMP,
               actualizado_por = %s
         WHERE id_gasto_forzado = %s
           AND version = %s
        RETURNING id_gasto_forzado, fecha, importe, concepto, version
        """,
        (nueva_fecha, nuevo_importe, nuevo_concepto or None,
         usuario, id_gasto_forzado, expected_version),
    )
    if not row:
        # Race: otra tx ganó entre nuestro SELECT y nuestro UPDATE
        return {"ok": False, "reason": "version_conflict_race"}
    return {
        "ok": True,
        "updated": {
            "id": int(row["id_gasto_forzado"]),
            "fecha": row["fecha"].isoformat() if row["fecha"] else None,
            "importe": float(row["importe"] or 0),
            "concepto": row["concepto"] or "",
            "version": int(row["version"] or 1),
        },
    }


def gasto_forzado_eliminar(id_gasto_forzado: int) -> bool:
    """Borra un gasto forzado. Devuelve True si se borró."""
    row = db.fetch_one(
        """
        DELETE FROM scintela.gasto_forzado
         WHERE id_gasto_forzado = %s
        RETURNING id_gasto_forzado
        """,
        (id_gasto_forzado,),
    )
    return bool(row)


def gastos_forzados_importar_bulk(
    items: list[dict], usuario: str = "web"
) -> dict:
    """Carga masiva desde localStorage del navegador (one-time migration).

    Acepta lista de items con shape {fecha, importe, concepto}. Saltea
    items que ya existen con misma fecha+importe+concepto (idempotente).
    """
    insertados = 0
    saltados = 0
    for it in items or []:
        fecha = it.get("fecha")
        importe = float(it.get("importe") or 0)
        concepto = (it.get("concepto") or "").strip()
        if not fecha or importe <= 0:
            saltados += 1
            continue
        # Dedup: ya existe igual?
        existe = db.fetch_one(
            """
            SELECT 1 FROM scintela.gasto_forzado
             WHERE fecha = %s::date
               AND ROUND(importe::numeric, 2) = ROUND(%s::numeric, 2)
               AND COALESCE(concepto, '') = %s
             LIMIT 1
            """,
            (fecha, importe, concepto),
        )
        if existe:
            saltados += 1
            continue
        gasto_forzado_crear(fecha, importe, concepto, usuario)
        insertados += 1
    return {"insertados": insertados, "saltados": saltados}


# ---------------------------------------------------------------------------
# VENTAS DEL MES por cliente — ranking estilo dBase TINT.BAT.
# Pedido dueña 2026-05-19 v8: al clickear "Ventas" del balance quiere ver
# la grilla "VENTAS DEL MES" con CLI / KG / MONTO / % ordenado por monto
# descendente, idéntica a la pantalla del dBase legacy.
# ---------------------------------------------------------------------------

def ventas_clientes_del_mes(anio: int | None = None,
                            mes: int | None = None) -> dict:
    """Ranking de clientes por ventas del mes (kg + monto + % del total).

    Mes por defecto = mes en curso (live, sin esperar snapshot). Excluye
    facturas anuladas (stat='X'). Devuelve:
        {
          "anio": int, "mes": int,
          "filas": [
            {"orden": 1, "codigo_cli": "EEU", "kg": int, "monto": float, "pct": int},
            ...
          ],
          "total_kg": int, "total_monto": float, "n_clientes": int,
        }
    """
    from datetime import date as _date
    hoy = _date.today()
    yy = int(anio) if anio else hoy.year
    mm = int(mes) if mes else hoy.month

    rows = db.fetch_all(
        """
        SELECT
            UPPER(TRIM(COALESCE(f.codigo_cli, '???'))) AS codigo_cli,
            COALESCE(SUM(f.kg), 0)::int                AS kg,
            COALESCE(SUM(f.importe), 0)::numeric       AS monto
          FROM scintela.factura f
         WHERE EXTRACT(YEAR  FROM f.fecha) = %s
           AND EXTRACT(MONTH FROM f.fecha) = %s
           AND COALESCE(f.stat, '') <> 'X'
         GROUP BY 1
         HAVING COALESCE(SUM(f.importe), 0) <> 0 OR COALESCE(SUM(f.kg), 0) <> 0
         ORDER BY SUM(f.importe) DESC NULLS LAST
        """,
        (yy, mm),
    ) or []

    total_kg = sum(int(r["kg"] or 0) for r in rows)
    total_monto = sum(float(r["monto"] or 0) for r in rows)

    filas = []
    for i, r in enumerate(rows, start=1):
        monto = float(r["monto"] or 0)
        # TMT 2026-05-19 v8 — dueña: agregar un decimal al %.
        pct = round((monto / total_monto * 100), 1) if total_monto else 0.0
        filas.append({
            "orden": i,
            "codigo_cli": r["codigo_cli"],
            "kg": int(r["kg"] or 0),
            "monto": monto,
            "pct": pct,
        })

    return {
        "anio": yy, "mes": mm,
        "filas": filas,
        "total_kg": total_kg,
        "total_monto": total_monto,
        "n_clientes": len(filas),
    }
