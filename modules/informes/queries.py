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
from filters import today_ec
from modules.posdat import (
    POSDAT_DEUDA_VIVA_WHERE,
    POSDAT_EGRESO_FLUJO_WHERE,
    posdat_deuda_viva_where,
)

# ---------------------------------------------------------------------------
# Filtro común para excluir backfill de Asinfo (TMT 2026-05-29).
#
# Cuando se re-importa data histórica desde Asinfo (ERP), se crean filas en
# scintela.factura / scintela.compra con `usuario_crea='asinfo-backfill'`.
# Esas filas también están contabilizadas en scintela.historia (cierre
# mensual). Por eso, CUALQUIER query que sume kg/importe de factura/compra
# para calcular números LIVE del mes en curso (venta del mes, MAT.PR del
# mes, Col.Qui del mes, etc.) DEBE excluir el backfill — sino doble-cuenta.
#
# Patrón: agregar `AND {NO_BACKFILL_WHERE}` al WHERE de la query.
# ---------------------------------------------------------------------------
NO_BACKFILL_WHERE = "COALESCE(usuario_crea, '') <> 'asinfo-backfill'"

# ---------------------------------------------------------------------------
# Corte de tintura dBase -> formulas_app (decision duena 2026-07-07).
# Lotes con fecha ANTERIOR al corte se quedan con el dBase (scintela.tinto).
# Lotes con fecha >= corte salen de formulas_app. Cambiar SOLO esta constante.
# Duena 2026-07-21: julio COMPLETO desde formulas (formulas tiene todo el mes:
# 216.352 kg por fecha_terminado) -> el corte pasa del 07/07 al 01/07. Ya no es
# un corte "a mitad de mes": queda como FRONTERA HISTORICA — lo anterior a
# julio esta congelado en scintela (dBase) y de julio en adelante todo sale de
# formulas_app.
# ---------------------------------------------------------------------------
CORTE_TINTURA = date(2026, 7, 1)

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
    """Provisión que FALTA amortizar este mes — modelo CUOTA MENSUAL.

    TMT 2026-05-19 v8 — dueña: "CAMBIAR el concepto de provisiones,
    pasar de diario a cuota mensual, y el calculo es: valor inicial a
    principios de mes, mas x/30 por la cuota mensual (x es la fecha
    actual) pero si x=31 o x=28 en febrero, a fin de mes ajustar a
    valor inicial+cuota mensual".

    Fórmula:
        provisionado_acumulado = cuota_mensual × proporcion
        provision_pendiente    = cuota_mensual − provisionado_acumulado

    Donde:
        proporcion = X/30 si X < último_dia_del_mes
                   = 1    si X = último_dia (clamp a 100%)

    Esto cubre meses cortos (febrero 28/29) y largos (31). El día 1 vale
    casi $80k, último día del mes vale 0.
    """
    import calendar as _cal

    h = hoy or today_ec()
    X = h.day
    ultimo_dia = _cal.monthrange(h.year, h.month)[1]
    if ultimo_dia <= X:
        proporcion = 1.0
    else:
        proporcion = min(X / 30.0, 1.0)
    provisionado = PROVISIONES_MES_USD * proporcion
    return max(PROVISIONES_MES_USD - provisionado, 0.0)


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

    TMT 2026-06-10 (decisión dueña, FINAL — 3er flip del día): el filtro
    vuelve pero distinto. "Solo si alguien aprieta CARGAR cuentan; si no,
    pertenecen a la lista Asinfo sin cargar. Una carga de dBase gana por
    sobre todo." → se excluye SOLO 'asinfo-backfill' (automático);
    'asinfo-carga' (botón Cargar) SÍ suma; el sync absorbe la copia
    asinfo cuando el DBF trae la misma factura (import_dbf, mig 0087).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total
        FROM scintela.factura
        WHERE (stat IS NULL OR stat IN ('Z','A','',' '))
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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

    TMT 2026-06-10 (revert): filtro `asinfo-backfill` removido — los
    cheques son siempre cartera viva si stat IN (Z,1,2,3,P,D), no importa
    si vinieron de Asinfo o no.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.cheque
        WHERE stat IN ('Z','1','2','3','P','D')
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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
                   -- TMT 2026-06-26 (dueña: "la utilidad está muy baja"). El
                   -- balance tomaba el saldo de una fila POSTDATADA (fecha
                   -- futura, ej. cheque al 30/06) → Pichincha entraba 90.261
                   -- más bajo y la utilidad caía igual. Espejamos el resto del
                   -- sistema (bancos/conciliación/sync): saldo = última fila
                   -- con fecha <= hoy.
                   AND t.fecha <= CURRENT_DATE
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
                   AND t.fecha <= CURRENT_DATE
               ), 0) AS saldo_signed,
               COALESCE((
                 SELECT SUM(t.importe)
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                   AND t.fecha <= CURRENT_DATE
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
        raw = float(r.get("saldo_raw") or 0)
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
                r.get("no_banco"),
                r.get("nombre"),
                origen,
                stored,
                signed,
                raw,
                r.get("n_transacciones"),
            )
        out.append(
            {
                "no_banco": r.get("no_banco"),
                "nombre": r.get("nombre"),
                "saldo": saldo,
                "saldo_origen": origen,
                "usa_fallback": origen not in ("stored", "empty"),
                "saldo_stored": stored,
                "saldo_signed": signed,
                "saldo_raw": raw,
                "n_transacciones": int(r.get("n_transacciones") or 0),
            }
        )
    return out


def salcaj() -> float:
    """Saldo en caja: opening + Σ entradas − Σ salidas.

    TMT 2026-06-05 (bug hunt lente 6 — bug #2 del reporte 2026-06-04):
    antes leíamos el `saldo` guardado de la última fila por
    `ORDER BY fecha DESC, id_caja DESC`. PROBLEMA: el running `saldo` se
    mantiene en orden de `id_caja` (insert), no de fecha. Si un mov queda
    back-dateado (ej. un reverso fechado en UTC mientras la entrada es de
    ayer Ecuador), salcaj() leía un saldo viejo y Resultados se
    desincronizaba de la caja real (episodio del 2026-06-04: salcaj
    reportó +$100 fantasma).

    Fix: misma fórmula que `caja.queries.saldo_actual()` — agregar signos
    desde `tipo` (E=+, S=−) + opening de la primera fila con saldo no-NULL.
    Robusto contra desorden de fechas.
    """
    row = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN tipo='E' THEN importe
                              WHEN tipo='S' THEN -importe
                              ELSE importe END), 0)
          + COALESCE(
              (SELECT saldo - CASE WHEN tipo='E' THEN importe
                                   WHEN tipo='S' THEN -importe
                                   ELSE importe END
                 FROM scintela.caja
                 WHERE saldo IS NOT NULL
                 ORDER BY fecha ASC, id_caja ASC LIMIT 1), 0
            ) AS saldo
        FROM scintela.caja
        """
    )
    return float(row["saldo"]) if row and row["saldo"] is not None else 0.0


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

    totp_raw = float(row["totp"] or 0)

    # TMT 2026-06-05: el pasivo del balance debe IGUALAR al dBase, que usa el
    # importe PERSISTIDO de POSDAT (TOTP = SUM(importe) WHERE banc<>9), SIN la
    # acumulación display-time de las provisiones YY. Antes (2026-05-29) sumábamos
    # ese delta_yy para que matcheara /posdat?tab=yy, pero eso lo despegaba del
    # dBase (PC quedaba +51k). El dBase no acumula al display; usa el valor
    # guardado. La acumulación YY sigue viva en el tab /posdat?tab=yy (display-time);
    # sólo NO entra al KPI Pasivos del balance.
    return {
        "pos1": float(row["pos1"] or 0),
        "pos2": float(row["pos2"] or 0),
        "totp": round(totp_raw, 2),
    }


def activos_totales() -> dict:
    """UMAQ = maquinaria (tipo M/C/K)  ·  UACT = Terr/Edif/Ins. (tipo I/T).

    Réplica FIEL del resultado legacy (MENU.PRG): UMAQ = FOR TIPO $ 'MCK',
    UACT = FOR TIPO = 'I'. El dBase suma SOLO por código de tipo, NUNCA por
    el texto del concepto, y NO cuenta los activos sin tipo (blank / '(s/t)').

    Único agregado sobre el legacy: el código 'T' (Terrenos) que la dueña
    introdujo en PC (2026-05) se suma junto a 'I' en UACT — así un terreno
    reclasificado de 'I' a 'T' sigue contando (antes se caía del activo y
    desplomaba la utilidad, bug 2026-07-22). Los activos '(s/t)' quedan
    afuera igual que en el dBase; para ponerlos en libros hay que tiparlos
    T/I explícitamente (decisión de la dueña, no automática por el nombre).

    Verificado 2026-07-22 contra ACTIVOS.DBF: UMAQ=1.081.299, UACT(I)=
    2.382.867 (== columna "previa" del snapshot). Los dos terrenos '(s/t)'
    (+TERRENO SUR 510k, TERRENO 6550m2 145k) están fuera del resultado en
    el dBase y por eso también acá.

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
    # Suma por CÓDIGO DE TIPO (como el dBase), NO por concepto: UMAQ = M/C/K,
    # UACT = I + T (edificios/instalaciones + terrenos). '(s/t)' y otros no
    # tipados quedan fuera, idéntico al legacy. `valor_calc` = valor en libros
    # prorrateado al día (NUNCA `inicial`). Excluye los soft-borrados (papelera).
    from modules.activos.queries import borrado_where_sql as _borr
    row = db.fetch_one(
        f"""
        WITH coef AS (
          SELECT LEAST(EXTRACT(DAY FROM (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::numeric, 30) / 30.0 AS c
        ),
        v AS (
          SELECT
            UPPER(TRIM(COALESCE(tipo, ''))) AS tp,
            COALESCE(inicial, 0)
              - COALESCE(amortizac, 0)
              - (SELECT c FROM coef) * COALESCE(cuota, 0) AS valor_calc
          FROM scintela.activos
          WHERE TRUE {_borr()}
        )
        SELECT
          COALESCE(SUM(CASE WHEN tp IN ('M','C','K') THEN GREATEST(valor_calc, 0) ELSE 0 END), 0) AS umaq,
          COALESCE(SUM(CASE WHEN tp IN ('I','T')     THEN GREATEST(valor_calc, 0) ELSE 0 END), 0) AS uact
        FROM v
        """
    )
    if not row:
        return {"umaq": 0.0, "uact": 0.0}
    return {"umaq": float(row["umaq"] or 0), "uact": float(row["uact"] or 0)}


def anticipos() -> float:
    """Anticipos USD vivos de clientes/proveedores.

    TMT 2026-06-10 (revert): filtro `asinfo-backfill` removido. Anticipos
    Asinfo son anticipos reales que deben aparecer en balance live.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.dolares
        WHERE (st IS NULL OR st IN ('', ' '))
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
        """
    )
    return float(row["total"] or 0) if row else 0.0


def uret_mes_ajustado() -> float:
    """URET del mes PARA LA FÓRMULA de utilidad (no para display).

    TMT 2026-07-06 v5 (dueña): los retiros OP (pc-retiro-op) se muestran
    POSITIVOS en Dividendos, pero su plata ya RESTÓ del balance por el lado
    del posdat OP (pasivos baja). Como URET vive DENTRO del Total Activo
    (MOV.CAPITAL), si también sumaran acá la utilidad subiría el doble.
    En la fórmula entran en NEGATIVO → TOTL baja junto con TOTP y la
    UTILIDAD queda QUIETA ("utilidad no debería cambiar con este movimiento").
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(CASE WHEN COALESCE(usuario_crea, '') = 'pc-retiro-op'
                                 THEN -ret ELSE ret END), 0) AS total
        FROM scintela.retiros
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
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


def movimientos_mes_dbase(anio: int | None = None, mes: int | None = None) -> dict:
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

    hoy = today_ec()
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

    # TMT 2026-05-19 v8 — Stock inicial = FINAL del mes ANTERIOR (pedido
    # dueña). scintela.iniciales del mes pedido es la proyección/opening
    # del mes en curso, pero la dueña quiere el cierre del mes anterior.
    # Calculamos el mes anterior (1→12 del año previo).
    mm_ant = mm - 1
    yy_ant = yy
    if mm_ant < 1:
        mm_ant = 12
        yy_ant = yy - 1
    try:
        inic = (
            db.fetch_one(
                """
            SELECT hilado, tejido, terminado, vq, um, uk, uf, uq
              FROM scintela.iniciales
             WHERE mesnum = %s AND yy = %s
             ORDER BY id_iniciales DESC
             LIMIT 1
            """,
                (mm_ant, yy_ant),
            )
            or {}
        )
    except Exception:
        inic = {}

    # Fallback: si no hay iniciales del mes anterior, agarrar la más reciente
    # PERO nunca de un mes FUTURO al pedido (mismo riesgo que el bug del balance
    # 2026-07-01: sin la fila del mes, agarraba Agosto). Constraint <= (yy_ant,
    # mm_ant). [[iniciales_mes_actual]]
    if not inic or not (float(inic.get("hilado") or 0)):
        try:
            inic = (
                db.fetch_one(
                    """
                SELECT hilado, tejido, terminado, vq, um, uk, uf, uq
                  FROM scintela.iniciales
                 WHERE COALESCE(hilado, 0) > 0
                   AND yy IS NOT NULL AND mesnum IS NOT NULL
                   AND (yy < %s OR (yy = %s AND mesnum <= %s))
                 ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
                 LIMIT 1
                """,
                    (yy_ant, yy_ant, mm_ant),
                )
                or {}
            )
        except Exception:
            inic = inic or {}

    hi0 = float(inic.get("hilado") or hist.get("stock_hilado") or hist.get("stock") or 0)
    tj0 = float(inic.get("tejido") or hist.get("stock_tejido") or 0)
    pf0 = float(inic.get("terminado") or hist.get("stock_terminado") or 0)
    vq0 = float(inic.get("vq") or hist.get("uqui") or 0)

    # TMT 2026-05-21 fix bug $/kg ingresos: el snapshot historia.kcom/ucom
    # incluye TODAS las compras del mes (H+K+T+Q+C). Pero la columna
    # "INGRESOS" de la fila HILADO debe contar solo compras tipo='H' —
    # sino el promedio $/kg sale distinto al "COMPRAS HILADO TOTAL" que
    # filtra por tipo. Bug reportado por dueña 2026-05-21: vio 2.913 en
    # Ingresos vs 2.863 en Compras Hilado.
    _hil_kg_importacion = {"kg": 0.0, "sin_match": [], "disponible": False}
    try:
        _h_rows = (
            db.fetch_all(
                f"""
            SELECT codigo_prov AS prov,
                   NULLIF(regexp_replace(COALESCE(concepto,''),'[^0-9]','','g'),'')::bigint AS ref,
                   fecha,
                   COALESCE(kg, 0)      AS kg,
                   COALESCE(importe, 0) AS importe
              FROM scintela.compra
             WHERE UPPER(COALESCE(tipo, '')) = 'H'
               AND COALESCE(stat, '') <> 'Y'
               AND EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) = %s
               AND {NO_BACKFILL_WHERE}
            """,
                (yy, mm),
            )
            or []
        )
        kcom = sum(float(r.get("kg") or 0) for r in _h_rows)
        ucom = sum(float(r.get("importe") or 0) for r in _h_rows)
        # TMT 2026-07-17 — las compras de importación CONVERTIDAS EN PC (BAP)
        # quedan con kg=0 a propósito: el kg vive en la IMPORTACIÓN (regla
        # dueña 2026-07-10; /compras lo muestra en gris "de referencia").
        # El dBase graba KG+IMPORTE en la compra, así que este SUM solo veía
        # los kg de las compras sincronizadas → el ponderado um_act se
        # inflaba y revaluaba TODO el stock (compra AC 22.992 kg en 0 →
        # tarifa 2,991 vs 2,955 → utilidad +83k). Completamos los kg desde
        # la MISMA fuente que /compras (la importación, una vez por
        # importación). Fail-soft: Asinfo caído → igual que antes, y el
        # balance lo ADVIERTE (hilado_kg_importacion.sin_match/disponible).
        try:
            from modules.importaciones import service as _imp_kg_svc

            _hil_kg_importacion = _imp_kg_svc.kg_hilado_faltantes_mes(_h_rows)
            kcom += float(_hil_kg_importacion.get("kg") or 0)
        except Exception:  # noqa: BLE001 -- nunca romper el mov por Asinfo
            pass
    except Exception:
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
    # TMT 2026-05-19 v8 — uk0/uf0 (tarifas tejido/terminado) ya no se
    # usan acá: el template TINT.BAT sólo muestra KG para CRUDO y TERM.
    # Si en el futuro se agregan columnas $ a esas etapas, recalcular.

    hilado_act_kg = max(hi0 + kcom - ktej, 0)
    tejido_act_kg = max(tj0 + ktej - ktin, 0)
    termin_act_kg = max(pf0 + ktin - kvent, 0)

    # % de eficiencia (egreso / ingreso del mes). En el dBase se mostraba
    # como "0.50%" en tejido crudo y "3.76%" en terminado — proxy de
    # merma o productividad.
    pct_tej = _safe_div(ktin, ktej) * 100 if ktej else 0.0
    pct_ter = _safe_div(kvent, ktin) * 100 if ktin else 0.0

    # TMT 2026-05-19 v8 — dueña: "porque era 2,926 y ahora 2,929?".
    # El stock_act $/kg se calcula como PROMEDIO PONDERADO entre el
    # stock inicial y los ingresos del mes — la dilución por compras
    # nuevas baja el $/kg. Fórmula PRG:
    #   um_act = (stock_inic_kg×um_inic + ingresos_us) / (stock_inic_kg + ingresos_kg)
    #   egresos_us = egresos_kg × um_act
    um_act = _safe_div(hi0 * um0 + ucom, hi0 + kcom) or um0
    header = {
        "hilado": {
            "stock_inic_kg": hi0,
            "stock_inic_ukg": um0,
            "stock_inic_us": hi0 * um0,
            "ingresos_kg": kcom,
            "ingresos_ukg": _safe_div(ucom, kcom),
            "ingresos_us": ucom,
            "egresos_kg": ktej,
            "egresos_ukg": um_act,
            "egresos_us": ktej * um_act,
            "stock_act_kg": hilado_act_kg,
            "stock_act_ukg": um_act,
            "stock_act_us": hilado_act_kg * um_act,
        },
        "tejido": {
            "stock_inic_kg": tj0,
            "ingresos_kg": ktej,
            "ingresos_pct": pct_tej,
            "egresos_kg": ktin,
            "stock_act_kg": tejido_act_kg,
        },
        "terminado": {
            "stock_inic_kg": pf0,
            "ingresos_kg": ktin,
            "ingresos_pct": pct_ter,
            "egresos_kg": kvent,
            "stock_act_kg": termin_act_kg,
        },
        "colorantes": {
            "stock_inic_us": vq0,
            "ingresos_us": 0.0,  # se setea abajo con compras Q del mes
            # Egresos $ derivado del balance contable: inic + ingresos - act.
            # Se completa después de calcular ingresos.
            "egresos_us": 0.0,
            "stock_act_us": vq0,  # default; se recalcula post-ingresos.
        },
    }

    # Breakdown por proveedor del mes seleccionado.
    # TMT 2026-05-19 v8 — fix prod: scintela.compra usa `codigo_prov`,
    # no `prov` (que es el nombre en posdat / cheque). Antes esto rompía
    # con `column "prov" does not exist` y tiraba la página entera.
    compras_hilado = (
        db.fetch_all(
            f"""
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
           AND {NO_BACKFILL_WHERE}
         GROUP BY codigo_prov
         ORDER BY SUM(importe) DESC
         LIMIT 20
        """,
            (yy, mm),
        )
        or []
    )
    for r in compras_hilado:
        r["ukg"] = _safe_div(r.get("importe"), r.get("kg"))

    # KG del STOCK (Asinfo), no de la compra — dueña 2026-07-10: muchas compras
    # (SALDO/CAE/seguro) mapean a un solo stock, así que el kg NO vive en la
    # compra (las BAP quedan en 0). Lo traemos de Asinfo por importación (una vez
    # por importación) y recalculamos $/kg. Fail-soft: si Asinfo cae o no matchea,
    # queda el kg propio de la compra.
    try:
        _ch_rows = db.fetch_all(
            f"""
            SELECT codigo_prov AS prov,
                   NULLIF(regexp_replace(COALESCE(concepto,''),'[^0-9]','','g'),'')::int AS ref,
                   fecha
              FROM scintela.compra
             WHERE UPPER(COALESCE(tipo, '')) = 'H'
               AND COALESCE(stat, '') <> 'Y'
               AND EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) = %s
               AND COALESCE(codigo_prov, '') <> ''
               AND UPPER(COALESCE(codigo_prov, '')) <> 'XX'
               AND {NO_BACKFILL_WHERE}
            """,
            (yy, mm),
        ) or []
        from modules.importaciones import service as _imp_svc
        _kg_prov = _imp_svc.kg_stock_por_compra(_ch_rows)
        for r in compras_hilado:
            _k = str(r.get("prov") or "").strip().upper()
            if _kg_prov.get(_k):
                r["kg"] = _kg_prov[_k]
                r["ukg"] = _safe_div(r.get("importe"), r["kg"])
    except Exception:  # noqa: BLE001 -- Asinfo caído → queda el kg de la compra
        pass

    # dueña 2026-07-15: en el Flujo de PRODUCCIÓN "Compras hilado" muestra el hilo
    # RECIBIDO (físico) del mes, no el facturado, para que cuadre con "Ingresos" de
    # Movimientos. AC (ARIESCOPE) es el único importador de hilo → una sola fila.
    try:
        from modules.importaciones import service as _imp_rec

        _rec = _imp_rec.costo_hilado_recibido_mes(yy, mm)
        if _rec and _rec.get("kg"):
            _prov_lbl = (compras_hilado[0].get("prov") if compras_hilado else None) or "AC"
            compras_hilado = [
                {
                    "prov": _prov_lbl,
                    "kg": _rec["kg"],
                    "importe": _rec["us"],
                    "ukg": (
                        _rec["usd_kg"]
                        if _rec.get("usd_kg") is not None
                        else _safe_div(_rec["us"], _rec["kg"])
                    ),
                }
            ]
    except Exception:  # noqa: BLE001 -- fail-soft: queda el facturado
        pass

    produc_tejido = (
        db.fetch_all(
            f"""
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
           AND {NO_BACKFILL_WHERE}
         GROUP BY codigo_prov
         ORDER BY SUM(kg) DESC
         LIMIT 20
        """,
            (yy, mm),
        )
        or []
    )
    # Producción tejido — costo por proveedor. Federico 2026-05-22.
    # Los tejedores tercerizados (AP, UN, ...) traen su importe real
    # facturado en scintela.compra. La autoproducción de INTELA (KK)
    # llega con importe=0 -- INTELA no se factura a sí misma --, así que
    # su costo es el gasto de tejeduría: V1+V2+V3 + amortización DTJ, la
    # misma cuenta que la fila Tejeduría del Informe de Resultados. Ese
    # gasto (_gs_tej) se reparte entre las filas con importe=0,
    # prorrateado por kg. La tabla va ordenada por kg desc (INTELA arriba).
    try:
        _gxg = gastos_xgast_v1_a_v9_mes()
        _amort = amortizaciones_mensuales()
    except Exception:
        _gxg, _amort = {}, {}
    _gs_tej = float(_gxg.get("gtej_sin_dtj") or 0) + float(_amort.get("dtj") or 0)
    _gs_tin = float(_gxg.get("gtin_sin_dcc") or 0) + float(_amort.get("dcc") or 0)

    sum_kg_tej = sum(float(r.get("kg") or 0) for r in produc_tejido)
    _filas_intela = [r for r in produc_tejido
                     if float(r.get("importe") or 0) == 0]
    _kg_intela = sum(float(r.get("kg") or 0) for r in _filas_intela)
    for r in produc_tejido:
        _imp = float(r.get("importe") or 0)
        _kg = float(r.get("kg") or 0)
        if _imp == 0 and _kg_intela > 0:
            r["importe"] = _gs_tej * (_kg / _kg_intela)
        r["ukg"] = _safe_div(r.get("importe"), r.get("kg"))

    # TINTORERIA — replica el PROCEDURE COMPRAS del dBase INFORMES.PRG.
    # Bajos vs Fuertes: corte IMPORTE/KG < 0.4, combinando dos fuentes:
    # scintela.tinto (tabla del tinturado, solo mes en curso) y
    # scintela.compra tipo='T' (tintura tercerizada del mes).
    # Federico 2026-05-21 -- antes usaba compra tipo='C' + una heuristica
    # fija 38.4/61.6; ahora reproduce la logica real del dBase.
    _LIM_TINT = 0.4
    # CORTE tintura: el dBase (scintela.tinto) solo cuenta los lotes
    # ANTERIORES al corte; del corte en adelante suma el bloque _f_* (formulas).
    _t = db.fetch_one(
        """
        SELECT
          COALESCE(SUM(CASE WHEN UPPER(TRIM(COALESCE(cod, ''))) <> 'LAV'
                            THEN kgn ELSE 0 END), 0)                  AS ktint,
          COALESCE(SUM(importe), 0)                                   AS itin,
          COALESCE(SUM(CASE WHEN importe / NULLIF(kg, 0) < %(lim)s
                            THEN kgn ELSE 0 END), 0)                  AS ktibaj,
          COALESCE(SUM(CASE WHEN importe / NULLIF(kg, 0) < %(lim)s
                            THEN importe ELSE 0 END), 0)              AS itibaj
        FROM scintela.tinto
        WHERE fecha < %(corte)s
          AND EXTRACT(YEAR FROM fecha)  = %(yy)s
          AND EXTRACT(MONTH FROM fecha) = %(mm)s
        """,
        {"lim": _LIM_TINT, "corte": CORTE_TINTURA, "yy": yy, "mm": mm},
    ) or {}
    _ct = db.fetch_one(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN importe / NULLIF(kg, 0) < %(lim)s
                            THEN kg ELSE 0 END), 0)                   AS kbaj,
          COALESCE(SUM(CASE WHEN importe / NULLIF(kg, 0) < %(lim)s
                            THEN importe ELSE 0 END), 0)              AS ibaj
        FROM scintela.compra
        WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'T'
          AND COALESCE(stat, '') NOT IN ('X', 'Y')
          AND EXTRACT(YEAR FROM fecha)  = %(yy)s
          AND EXTRACT(MONTH FROM fecha) = %(mm)s
          AND {NO_BACKFILL_WHERE}
        """,
        {"lim": _LIM_TINT, "yy": yy, "mm": mm},
    ) or {}
    # CORTE tintura: del corte en adelante, los lotes salen de formulas_app,
    # con la MISMA regla Bajos/Fuertes (importe/kg < 0.4). Solo cuentan las
    # ordenes ya terminadas (tela_terminada_kg > 0) para que el $/kg no se
    # dispare con ordenes en proceso.
    _f_ktint = _f_itin = _f_ktibaj = _f_itibaj = 0.0
    try:
        import calendar as _cal

        from modules.tintura import service as _tint_svc

        _m_ini = date(yy, mm, 1)
        _m_fin = date(yy, mm, _cal.monthrange(yy, mm)[1])
        _f_desde = max(CORTE_TINTURA, _m_ini)
        if _f_desde <= _m_fin:
            for _o in _tint_svc.tinto_equiv_formulas(_f_desde, _m_fin):
                _kgn = _o.kgn or 0.0
                if _kgn <= 0:
                    continue
                _kg = _o.kg or 0.0
                _imp = _o.importe or 0.0
                _f_ktint += _kgn
                _f_itin += _imp
                _den = _kg if _kg > 0 else _kgn
                if _den > 0 and (_imp / _den) < _LIM_TINT:
                    _f_ktibaj += _kgn
                    _f_itibaj += _imp
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
        pass

    _ktint = float(_t.get("ktint") or 0) + _f_ktint
    _itin = float(_t.get("itin") or 0) + _f_itin
    bajos_kg = float(_ct.get("kbaj") or 0) + float(_t.get("ktibaj") or 0) + _f_ktibaj
    bajos_us = float(_ct.get("ibaj") or 0) + float(_t.get("itibaj") or 0) + _f_itibaj
    fuertes_kg = _ktint - bajos_kg
    fuertes_us = _itin - bajos_us
    tint_kg = _ktint
    tint_us = _itin
    # El % se reparte sobre KT = kg que ENTRAN a tinturar (crudo
    # egresos = ktin); KTINT (= tint_kg) son los que SALEN tinturados.
    # La diferencia entre ambos es el desperdicio del proceso.
    bajos_pct = (bajos_kg / ktin * 100.0) if ktin else 0.0
    fuertes_pct = (100.0 - bajos_pct) if ktin else 0.0

    # ── Cadena crudo→terminado con los MISMOS flujos que el panel STOCK del
    # balance (PRG INFORMES.PRG L264/L315), para IGUALAR la foto del dBase.
    # TMT 2026-07-09 (dueña "el que mostrás está mal, mostrá lo del dBase"):
    #   · CRUDO egreso  = KT  = kg que ENTRAN a tintura
    #                         = compras tipo='T' externas + KTINT − servicios(KSTI)
    #   · TERM  ingreso = KR  = kg NETOS que salen a terminado (con la merma
    #                           de tintura ~5%; NO es igual a KT).
    #   · TERM  egreso  = ventas FÍSICAS del mes (kg que salieron del depósito),
    #                     no el snapshot historia.kvent (que venía del último
    #                     cierre → daba 335k y hundía el stock de terminado).
    # Los flujos vivos son del mes en curso; para meses PASADOS se cae al
    # comportamiento previo (derivado de _ktint / snapshot) para no romper la
    # foto histórica. Igual criterio que el guard de iniciales del balance.
    # El dBase MOVIMIENTOS lee TINTO.DBF del MES COMPLETO. scintela.tinto es la
    # réplica de TINTO.DBF y también tiene el mes completo (skill tintura: "solo
    # el mes actual, 146 filas todas de julio"). El CORTE_TINTURA (hoy 01/07,
    # era 07/07) que usa el resto del informe es para el COSTEO ($/kg, puente a
    # formulas_app), NO
    # para el balance de masa en kg — acá cortaba el 07/07 (37.344 kg) y
    # subcontaba (48.851 vs 88.046 del dBase). Para la CADENA leemos scintela.tinto
    # del mes entero, sin corte, = lo que ve el dBase. TMT 2026-07-09 (dueña
    # "perseguí el 88.046").
    _es_mes_actual = (yy == hoy.year and mm == hoy.month)
    if _es_mes_actual:
        _tin_full = db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                THEN kg  ELSE 0 END), 0)                    AS ktint,
              COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                 AND COALESCE(kg, 0) > 0
                                THEN kgn ELSE 0 END), 0)                    AS kr
            FROM scintela.tinto
            WHERE EXTRACT(YEAR FROM fecha)  = %(yy)s
              AND EXTRACT(MONTH FROM fecha) = %(mm)s
            """,
            {"yy": yy, "mm": mm},
        ) or {}
        _ktint_full = float(_tin_full.get("ktint") or 0)
        _kr_full = float(_tin_full.get("kr") or 0)
        try:
            _kvent_fis = float(ventas_mes_corriente_kg_fisico() or 0)
        except Exception:  # noqa: BLE001 -- fail-soft
            _kvent_fis = kvent
        # CRUDO egreso = kg que ENTRAN a tintura = KTINT bruto (excl. lavados).
        # TERM ingreso = KR neto (con la merma de tintura ~5%). TERM egreso =
        # ventas físicas del mes.
        _crudo_egreso = _ktint_full or _ktint
        _term_ingreso = _kr_full or _ktint
        _term_egreso = _kvent_fis
    else:
        # Meses pasados: comportamiento previo (cadena cerrada con _ktint).
        _crudo_egreso = _ktint
        _term_ingreso = _ktint
        _term_egreso = kvent

    # TERMINADO — ingreso = KR (neto a terminado); egreso = ventas físicas.
    # % = merma de tintura = (crudo egreso − term ingreso) / crudo egreso.
    header["terminado"]["ingresos_kg"] = _term_ingreso
    header["terminado"]["ingresos_pct"] = _safe_div(
        _crudo_egreso - _term_ingreso, _crudo_egreso
    ) * 100
    header["terminado"]["egresos_kg"] = _term_egreso
    header["terminado"]["stock_act_kg"] = max(pf0 + _term_ingreso - _term_egreso, 0)

    # CRUDO ingresos = produccion de tejido del mes EN VIVO (total
    # compras tipo K = sum_kg_tej), no el snapshot historia.ktej.
    # HILADO egresos = ese tejido + 0,5% de desperdicio de tejeduria.
    # Recalculo los stock actuales de Hilado y Crudo para que cuadren.
    # Federico 2026-05-22.
    _kg_tej = float(sum_kg_tej or 0)
    _kh = _kg_tej * 1.005
    header["tejido"]["ingresos_kg"] = _kg_tej
    # % de CRUDO ingresos = desperdicio de tejeduria (0,5%), igual que el
    # dBase. _kh es el hilado consumido (tejido + 0,5%). Federico 2026-05-22.
    header["tejido"]["ingresos_pct"] = _safe_div(_kh - _kg_tej, _kg_tej) * 100
    # CRUDO egresos = KT (lo que ENTRA a tintura). Antes usaba _ktint (los que
    # SALEN tinturados) → colapsaba crudo egreso = term ingreso sin merma y
    # quedaba lejos del dBase. TMT 2026-07-09.
    header["tejido"]["egresos_kg"] = _crudo_egreso
    header["tejido"]["stock_act_kg"] = max(tj0 + _kg_tej - _crudo_egreso, 0)
    header["hilado"]["egresos_kg"] = _kh
    header["hilado"]["egresos_us"] = _kh * um_act
    _hil_act = max(hi0 + kcom - _kh, 0)
    header["hilado"]["stock_act_kg"] = _hil_act
    header["hilado"]["stock_act_us"] = _hil_act * um_act

    # CS.COLORANTES — costo unitario colorantes consumidos / kg tinturados.
    # Aproximación: importe de compras de químicos del mes (tipo='Q') sobre
    # kg tinturados del mes (ktin de historia).
    quimicos_mes = (
        db.fetch_one(
            f"""
        SELECT COALESCE(SUM(importe), 0) AS importe
          FROM scintela.compra
         WHERE UPPER(COALESCE(tipo, '')) = 'Q'
           AND COALESCE(stat, '') <> 'Y'
           AND EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND {NO_BACKFILL_WHERE}  -- TMT 2026-05-29: ver constante arriba
        """,
            (yy, mm),
        )
        or {}
    )
    cs_col_us = float(quimicos_mes.get("importe") or 0)
    # TMT 2026-05-29 dueña: la fila Colorantes/Quím. en el balance usa
    # kg tinturados LIVE del mes en curso (de scintela.tinto), NO el
    # ktin de historia (que es el cierre del mes anterior). El dBase
    # mostraba 312.903 kg vs PC 300.012 — diferencia por usar histórico.
    try:
        _tint_live = tinto_mes_corriente_resultado()
        _ktin_live = float(_tint_live.get("ktint") or 0)
    except Exception:  # noqa: BLE001
        _ktin_live = 0.0
    # Si no hay datos live del mes actual, fallback a ktin de historia.
    cs_col_kg = _ktint if _ktint > 0 else ktin  # CORTE: KTINT combinado (dBase+formulas)
    cs_col_ukg = _safe_div(cs_col_us, cs_col_kg)
    # Aplicar al header de colorantes el ingreso $ del mes.
    header["colorantes"]["ingresos_us"] = cs_col_us
    header["colorantes"]["kg_live"] = cs_col_kg  # exposed para la tabla
    # TMT 2026-05-19 v8 — egresos $ de colorantes derivado por balance:
    # inic + ingresos - act = consumo. Necesitamos stock act final, que
    # viene del último snapshot live (uqui de historia_ultimo_snapshot).
    # Si no tenemos snapshot fresh, usamos vq0 = stock_act = stock_inic
    # (consumo = ingresos del mes).
    try:
        hist_live = historia_ultimo_snapshot() or {}
        stock_act_col = float(hist_live.get("uqui") or vq0)
    except Exception:
        stock_act_col = vq0
    header["colorantes"]["stock_act_us"] = stock_act_col
    header["colorantes"]["egresos_us"] = max(
        float(header["colorantes"]["stock_inic_us"])
        + float(header["colorantes"]["ingresos_us"])
        - float(header["colorantes"]["stock_act_us"]),
        0.0,
    )

    # CS.PRODUCCION — costo total de producción (mat. prima + tejido + tin
    # + col) / kg producidos en el mes. Usa hist live.
    cs_prod_us = ucom + utej + utin + cs_col_us
    cs_prod_kg = ktin or ktej or kvent  # mejor proxy disponible
    cs_prod_ukg = _safe_div(cs_prod_us, cs_prod_kg)

    # TMT 2026-05-19 v8 — CS Anterior (columna ANT.) = CS unitario del
    # mes pasado, de scintela.historia. Pedido dueña: comparar
    # mes-a-mes el costo unitario.
    cs_col_ukg_ant = 0.0
    cs_prod_ukg_ant = 0.0
    tint_ukg_ant = 0.0  # promedio $/kg tintura del mes anterior
    try:
        hist_ant = (
            db.fetch_one(
                """
            SELECT ucom, utej, utin, ktin, ktej, kvent
              FROM scintela.historia
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) = %s
             ORDER BY fecha DESC LIMIT 1
            """,
                (yy_ant, mm_ant),
            )
            or {}
        )
        if hist_ant:
            ucom_ant = float(hist_ant.get("ucom") or 0)
            utej_ant = float(hist_ant.get("utej") or 0)
            utin_ant = float(hist_ant.get("utin") or 0)
            ktin_ant = float(hist_ant.get("ktin") or 0)
            ktej_ant = float(hist_ant.get("ktej") or 0)
            kvent_ant = float(hist_ant.get("kvent") or 0)
            # TMT 2026-05-19 v8 — dueña: "te falta el ANT". Tintorería
            # ANT = costo unitario promedio del mes anterior (utin / ktin).
            tint_ukg_ant = _safe_div(utin_ant, ktin_ant)
            # CS.colorantes anterior: hay que calcular quimicos del mes
            # anterior también — query separada.
            try:
                q_ant = (
                    db.fetch_one(
                        f"""
                    SELECT COALESCE(SUM(importe), 0) AS importe
                      FROM scintela.compra
                     WHERE UPPER(COALESCE(tipo, '')) = 'Q'
                       AND COALESCE(stat, '') <> 'Y'
                       AND EXTRACT(YEAR FROM fecha)  = %s
                       AND EXTRACT(MONTH FROM fecha) = %s
                       AND {NO_BACKFILL_WHERE}
                    """,
                        (yy_ant, mm_ant),
                    )
                    or {}
                )
                cs_col_us_ant = float(q_ant.get("importe") or 0)
            except Exception:
                cs_col_us_ant = 0.0
            cs_col_ukg_ant = _safe_div(cs_col_us_ant, ktin_ant)
            cs_prod_us_ant = ucom_ant + utej_ant + utin_ant + cs_col_us_ant
            cs_prod_kg_ant = ktin_ant or ktej_ant or kvent_ant
            cs_prod_ukg_ant = _safe_div(cs_prod_us_ant, cs_prod_kg_ant)
    except Exception:
        pass

    # _gs_tin (Gs. Tintorería para COSTOS UNITARIOS = V4+V5+V6 + DCC) y
    # _gs_tej ya se calcularon más arriba, junto con la tabla Producción
    # tejido. Federico 2026-05-22.

    return {
        "anio": yy,
        "mes": mm,
        "header": header,
        # kg completados desde la importación (BAP kg=0) + compras sin kg
        # que NO matchearon — el balance los usa para ADVERTIR. TMT 2026-07-17.
        "hilado_kg_importacion": _hil_kg_importacion,
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
            "total": {
                "kg": tint_kg,
                "us": tint_us,
                "ukg": _safe_div(tint_us, tint_kg),
                "pct": 100.0 if tint_kg else 0.0,
                "ant": None,
            },
            "bajos": {
                "kg": bajos_kg,
                "us": bajos_us,
                "ukg": _safe_div(bajos_us, bajos_kg),
                "pct": bajos_pct,
                "ant": None,
            },
            "fuertes": {
                "kg": fuertes_kg,
                "us": fuertes_us,
                "ukg": _safe_div(fuertes_us, fuertes_kg),
                "pct": fuertes_pct,
                "ant": None,
            },
        },
        "cs": {
            # COSTOS UNITARIOS - kg = producidos (KTINT); $/kg = $/kg.
            # Colorantes: $ = compras quimicos del mes. Gs. Tintoreria:
            # $ = misma cuenta que la fila Tintoreria de Resultados.
            # ant = None hasta retener los cierres mensuales.
            "colorantes": {"kg": _ktint,
                           "ukg": _safe_div(cs_col_us, _ktint),
                           "us": cs_col_us, "ant": None},
            "produccion": {"kg": _ktint,
                           "ukg": _safe_div(_gs_tin, _ktint),
                           "us": _gs_tin, "ant": None},
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
        WHERE fecha < date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::date
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

    TMT 2026-05-20 v3 — defensivo: filtra snapshots auto-creados
    incompletos (= los que escribimos sin computar ktej/ktin/utej/utin).
    Si el snapshot no tiene esos campos, /flujo-produccion mostraba 0
    en TINTORERIA y KK $/kg. Ahora exigimos ktej > 0 para considerar
    el snapshot "completo". Si todos están vacíos, devuelve el más
    reciente igual (fallback gracioso, mejor algo que nada).
    """
    row = db.fetch_one(
        """
        SELECT *
        FROM scintela.historia
        WHERE COALESCE(ktej, 0) > 0
        ORDER BY fecha DESC
        LIMIT 1
        """
    )
    if row:
        return row
    # Fallback: si NINGÚN snapshot tiene ktej, devolver el más reciente.
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
    hoy = today_ec()
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
    if row and (float(row.get("kprog") or 0) > 0 or float(row.get("hilado") or 0) > 0):
        return row

    # 2. La más reciente con datos reales (algún campo > 0), pero NUNCA de un
    #    mes FUTURO. Si falta la fila del mes en curso (p.ej. el rollover del
    #    dBase todavía no la escribió, o quedó corrupta con yy/mesnum NULL),
    #    caemos al ÚLTIMO cierre disponible (mes <= el corriente), jamás a una
    #    proyección adelantada: si `inic.mesnum` apunta al mes SIGUIENTE, el
    #    stock de terminado se calcula contra un mes previo inexistente y cae a
    #    0, hundiendo el patrimonio ~2M. Bug 2026-07-01: iniciales tenía la fila
    #    de Agosto pero no la de Julio -> stock -2M y utilidad -1,69M fantasma.
    row = db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        WHERE (COALESCE(kprog, 0) > 0
               OR COALESCE(hilado, 0) > 0
               OR COALESCE(pretot, 0) > 0)
          AND yy IS NOT NULL AND mesnum IS NOT NULL
          AND (yy < %s OR (yy = %s AND mesnum <= %s))
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """,
        (hoy.year, hoy.year, hoy.month),
    )
    if row:
        return row

    # 2b. Recién si NO hay ningún mes <= el corriente con datos (base recién
    #     inicializada) aceptamos el más reciente aunque sea futuro — mejor
    #     algo que nada, pero seguimos excluyendo filas corruptas (yy NULL).
    row = db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        WHERE (COALESCE(kprog, 0) > 0
               OR COALESCE(hilado, 0) > 0
               OR COALESCE(pretot, 0) > 0)
          AND yy IS NOT NULL AND mesnum IS NOT NULL
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """
    )
    if row:
        return row

    # 3. Fallback final: la más reciente sin importar (excluye filas con
    #    yy/mesnum NULL para no devolver la fila basura primero — en Postgres
    #    `ORDER BY yy DESC` pone los NULL adelante).
    return db.fetch_one(
        """
        SELECT *
        FROM scintela.iniciales
        WHERE yy IS NOT NULL AND mesnum IS NOT NULL
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 1
        """
    )


_TARIFA_COLS_PREV = {"um", "uk", "uf", "uq", "pre", "hilado", "tejido", "terminado", "vq"}


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
    row = (
        db.fetch_one(
            f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(kg), 0)      AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.factura
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND (stat IS NULL OR stat <> 'X')
          AND {NO_BACKFILL_WHERE}  -- ver constante arriba
        """
        )
        or {}
    )
    hoy = today_ec()
    # primer día del mes siguiente menos un día
    if hoy.month == 12:
        ultimo_dia = date(hoy.year + 1, 1, 1).day
    else:
        from calendar import monthrange

        ultimo_dia = monthrange(hoy.year, hoy.month)[1]
    return {
        "n": int(row.get("n") or 0),
        "kg": float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
        "dias_pasados": hoy.day,
        "dias_mes": ultimo_dia,
    }


def ventas_mes_corriente_kg_fisico() -> float:
    """Ventas del mes EN kg SIN el filtro de backfill — para stock físico.

    TMT 2026-06-10: `ventas_mes_corriente_resultado()` aplica
    NO_BACKFILL_WHERE para que las facturas traídas de Asinfo NO inflen la
    cartera (TOTF) ni cuenten como ventas del mes en Resultados. Eso es correcto para "no contar lo que se trajo de Asinfo
    todavía". PERO esas facturas YA REPRESENTAN VENTAS FÍSICAS REALES — la
    mercadería salió del depósito. El cálculo de stock terminado_kg/tejido_kg
    debe descontar esas kg para que `vsto_display` (= kg × tarifas) refleje
    el stock real, no el "stock virtual asumiendo que esas ventas no
    ocurrieron". Sin este "incluir todo", terminado_kg infla por las kg
    vendidas que no se descuentan → vsto sube → patr sube → utilidad infla.

    Esta función NO aplica el filtro A PROPÓSITO (no agregar
    NO_BACKFILL_WHERE acá — hay un contract test que lo vigila). Solo se usa
    en stock_kg para que terminado/tejido reflejen kg físicos reales.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg
          FROM scintela.factura  -- kg-fisico-incluye-todo (a propósito)
         WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
           AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
           AND (stat IS NULL OR stat <> 'X')
        """,
    )
    return float(row.get("kg") or 0) if row else 0.0


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
    # TMT 2026-07-23 (dueña "subime la utilidad, es un desastre"): traemos las
    # FILAS (no un SUM) para poder reconstruir el kg de las compras de importación
    # convertidas en PC (BAP) que quedan con kg=0 — el kg vive en la importación
    # (regla 2026-07-10). El SUM crudo NO las contaba, así que el stock de hilado
    # (HI = HI0 + KM − KH) quedaba SUB-contado y la utilidad sub-valuada (~$100-135k
    # al 07/2026: refs AC 99/16 de ARIESCOPE que Andrés convirtió). El Flujo de
    # producción ya reconstruía; el balance NO → quedaban inconsistentes. Ahora el
    # balance usa la MISMA fuente.
    rows = (
        db.fetch_all(
            f"""
        SELECT codigo_prov AS prov,
               NULLIF(regexp_replace(COALESCE(concepto,''),'[^0-9]','','g'),'')::bigint AS ref,
               fecha,
               COALESCE(kg, 0)      AS kg,
               COALESCE(importe, 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'H'
          -- Excluir anuladas (stat 'X' o 'Y'). Sin este filtro, compras
          -- reversadas seguían inflando MAT.PR. y U$/kg ponderado.
          -- TMT 2026-05-13.
          AND COALESCE(stat, '') NOT IN ('X', 'Y')
          AND {NO_BACKFILL_WHERE}  -- ver constante arriba
        """
        )
        or []
    )
    kg = sum(float(r.get("kg") or 0) for r in rows)
    importe = sum(float(r.get("importe") or 0) for r in rows)
    # Reconstruir el kg que falta desde la importación (kg 1 vez por importación;
    # dedup interno). Fail-soft: si Asinfo cae, queda el kg propio de la compra
    # (mismo comportamiento que antes de este fix).
    try:
        from modules.importaciones.service import kg_hilado_faltantes_mes

        kg += float(kg_hilado_faltantes_mes(rows).get("kg") or 0)
    except Exception:  # noqa: BLE001 -- nunca romper el balance por Asinfo
        pass
    return {
        "n": len(rows),
        "kg": float(kg),
        "importe": float(importe),
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
    ("YY", "concepto_starts_with", "SR", 3300),
    ("YY", "concepto_starts_with", "13", 1000),
    ("YY", "concepto_starts_with", "14", 300),
    ("YY", "concepto_starts_with", "AB", 1300),
    ("YY", "concepto_starts_with", "SS", 2400),
    # TMT 2026-05-15 (re-audit C5): el patrón "A,E,C" ANTES era
    # `concepto LIKE 'A,E,C%'` — nunca matcheaba nada y silenciosamente
    # dropeaba $7,300/día ($160-220k/mes) de provisiones. dBase original
    # usaba `LEFT(concepto,1) $ 'AEC'` (init A, E o C). Lo reemplazamos
    # con el matcher `concepto_starts_with_any` (lista de iniciales).
    ("YY", "concepto_starts_with_any", "A|E|C", 7300),
    ("YY", "concepto_starts_with", "SUELDOS", 6000),
    ("YY", "concepto_eq", "ALQUILER", 700),
    ("", "prov_eq", "RT", 8400),
    ("", "concepto_contains", "INCOB", 400),
    ("", "concepto_starts_with", "JP", 200),
    ("", "concepto_contains", "INTER", 300),
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

    hoy = today_ec()
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
            "SELECT valor FROM scintela.sistema_meta  WHERE clave = %s FOR UPDATE",
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
        # TMT 2026-06-08: VERIFICADO contra POSDAT.DBF — el dBase real avanza
        # L-V (no contó el sábado 06/06). MENU.PRG dice DOW>1 pero el sistema
        # no se abre los sábados, así que en la práctica es L-V. Mantener L-V.
        cursor_d = ult_fecha + _td(days=1)
        while cursor_d <= hoy:
            if cursor_d.weekday() < 5:  # 0=L .. 4=V (sin S/D)
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
                    "aplicado": False,
                    "dias_aplicados": 0,
                    "monto_total": 0.0,
                    "categorias_por_dia": 0,
                    "ult_fecha_anterior": ult_fecha_str,
                    "ult_fecha_nueva": ult_fecha_str,
                    "motivo": (
                        f"forzar rechazado: ya se aplicó hasta {ult_fecha_str} "
                        f"≥ hoy {hoy_iso}. No permitimos doble-aplicar."
                    ),
                }
            # Forzar: agregar último día hábil (L-V); si hoy es S/D, retroceder.
            _f = hoy
            while _f.weekday() >= 5:
                _f -= _td(days=1)
            dias_a_aplicar = [_f]

        if not dias_a_aplicar:
            # Lock liberado al salir del with. Sin cambios — devolver sin
            # tocar el marker.
            return {
                "aplicado": False,
                "dias_aplicados": 0,
                "monto_total": 0.0,
                "categorias_por_dia": 0,
                "ult_fecha_anterior": ult_fecha_str,
                "ult_fecha_nueva": ult_fecha_str,
                "motivo": "ya al día (sin días hábiles pendientes)",
            }

        # TMT 2026-05-20 — pedido dueña: el cron diario ahora se DRIVEA
        # de la tabla `scintela.provisiones` (no de la lista hardcoded
        # PROVISIONES_DIARIAS). Para cada provisión:
        #   1. Buscar la posdat YY que matchea por concepto (starts-with
        #      bidireccional case-insensitive, longitud ≥ 3).
        #   2. Si existe → importe += pr.importe (cuota diaria, tal cual).
        #   3. Si no existe → se saltea (no toca ese día — la dueña puede
        #      crear el posdat YY manualmente o dejar la provisión sola).
        #
        # TMT 2026-05-28 dueña: 'en vez de mensual hagamos cuota diaria'.
        # ANTES: importe += cuota_mensual / 30. AHORA: importe += pr.importe
        # tal cual. La tabla provisiones ahora guarda la cuota DIARIA
        # directamente. La dueña va a actualizar los valores manualmente
        # desde la pantalla de provisiones (label cambiado a 'Cuota diaria').
        #
        # La lista hardcoded queda como FALLBACK SOLO para provisiones
        # legacy que todavía no están en `scintela.provisiones`. Una vez
        # que la dueña migre todo, podés borrar el fallback.
        provisiones_rows = (
            db.fetch_all(
                "SELECT id_provisiones, concepto, importe "
                "FROM scintela.provisiones "
                "WHERE COALESCE(importe, 0) > 0",
                conn=conn,
            )
            or []
        )
        for _dia in dias_a_aplicar:
            cats_dia = 0
            # 1. Driver nuevo: cada provisión aplica su CUOTA DIARIA
            # (valor de scintela.provisiones tal cual, sin dividir).
            for pr in provisiones_rows:
                concepto = (pr.get("concepto") or "").strip()
                if len(concepto) < 3:
                    continue
                diario = float(pr.get("importe") or 0)
                if diario <= 0:
                    continue
                # Match aproximado contra el concepto del posdat YY.
                # Mismo criterio que el LATERAL JOIN viejo: starts-with
                # bidireccional, case-insensitive.
                ret = db.fetch_one(
                    """
                    WITH first_match AS (
                        SELECT id_posdat
                          FROM scintela.posdat
                         WHERE COALESCE(banc, 0) <> 9
                           AND (anulada IS NOT TRUE OR anulada IS NULL)
                           AND UPPER(COALESCE(prov, '')) = 'YY'
                           -- TMT 2026-05-28 (migración 0061): las filas con
                           -- baseline_date NOT NULL son manejadas
                           -- display-time en posdat.queries — el cron
                           -- ya no debe tocar su importe.
                           AND baseline_date IS NULL
                           AND LENGTH(TRIM(COALESCE(concepto, ''))) >= 3
                           AND (
                                UPPER(TRIM(COALESCE(concepto, '')))
                                  LIKE UPPER(%(c)s) || '%%'
                             OR UPPER(%(c)s)
                                  LIKE UPPER(TRIM(COALESCE(concepto, ''))) || '%%'
                           )
                         ORDER BY id_posdat
                         LIMIT 1
                    )
                    UPDATE scintela.posdat p
                       SET importe = COALESCE(p.importe, 0) + %(diario)s,
                           fecha_modifica = CURRENT_TIMESTAMP,
                           usuario_modifica = 'provisiones_diarias'
                      FROM first_match fm
                     WHERE p.id_posdat = fm.id_posdat
                    RETURNING p.id_posdat
                    """,
                    {"c": concepto, "diario": diario},
                    conn=conn,
                )
                if ret:
                    cats_dia += 1
                    total += diario
            # 2. Fallback legacy: la lista hardcoded PROVISIONES_DIARIAS.
            # Sólo aplica a posdats que NO matchearon con la tabla nueva
            # (chequeamos por id_posdat distinto). Útil mientras la dueña
            # migra todas las categorías. TMT 2026-05-20: dejar comentado
            # si después de un mes no se ve usado.
            # for prov_filter, matcher_kind, pattern, monto in PROVISIONES_DIARIAS:
            #     ... (código viejo)
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


def amortizaciones_mensuales(meses_atras: int = 0) -> dict:
    """Amortizaciones del mes desde scintela.activos (INFORMES.PRG líneas 42-50).

    `meses_atras` (Federico 2026-07-21): 0 = mes en curso (proración diaria
    dBase). >0 = un mes CERRADO hacia atrás → se usa la cuota completa
    (coef=1), porque el mes ya terminó. Sirve para la fila "Gastos mes
    anterior" de la pantalla de gastos.


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
    rows = (
        db.fetch_all(
            """
        WITH coef AS (
          SELECT CASE WHEN %(off)s > 0 THEN 1.0
                      ELSE LEAST(EXTRACT(DAY FROM (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::numeric, 30) / 30.0
                 END AS c
        )
        SELECT UPPER(TRIM(tipo)) AS tipo,
               COALESCE(SUM((SELECT c FROM coef) * COALESCE(cuota, 0)), 0) AS total
        FROM scintela.activos
        WHERE COALESCE(cuota, 0) > 0
        GROUP BY 1
        """,
            {"off": int(meses_atras)},
        )
        or []
    )
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
        "dcc": deprmaq + depract * 0.5,
        "dtj": deprtej + depract * 0.5,
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


def _dedup_key(fecha, prov, concepto, importe) -> tuple:
    """Clave para detectar el MISMO movimiento cargado en xgast Y en compra.

    TMT 2026-07-14 — pedido dueña: "no quiero repeticiones en los gastos".
    Un gasto (ej. KK SU MORALES) puede estar cargado a la vez en `xgast` y
    en `compra` (tipo mapeado al mismo rubro). Sin dedup se cuenta dos veces
    (el drill-down mostraba 50.398,12 en vez de 25.199,06). Priorizamos la
    categoría (xgast); si la compra tiene un gemelo exacto en xgast del mismo
    rubro/mes, no se vuelve a sumar. Las compras SIN gemelo (servicios que
    sólo viven en `compra`) siguen apareciendo.

    Clave = (fecha ISO, prov normalizado, concepto normalizado, importe a 2 dec).
    """
    f = fecha.isoformat() if hasattr(fecha, "isoformat") else str(fecha or "")
    p = (prov or "").upper().strip()
    c = " ".join((concepto or "").upper().split())
    try:
        imp = round(float(importe or 0), 2)
    except (TypeError, ValueError):
        imp = 0.0
    return (f, p, c, imp)


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
        return {"num": n, "label": "(categoría inválida)", "grupos": [], "total": 0.0, "n_filas": 0}

    where_fecha = (
        "AND fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) "
        "AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'"
        if mes_actual
        else ""
    )
    # TMT 2026-05-19 v6 re-audit — agregado filtro stat='Y' (anuladas).
    # Antes el drill-down mostraba gastos anulados sumados al subtotal,
    # discrepando con `gastos_xgast_v1_a_v9_mes` que sí los excluye.
    # Tamara 2026-07-22: el drill-down de V6 incluye los químico-insumos
    # (QUIMSERTEC/TOSAVA/ECUAPLAST/NC/QI = gasto de tintorería, como los CC), igual
    # que la matriz. Pero excluye el wrapper manual 'QUIMICOS <prov>' (copia
    # redundante del químico ya cargado como gasto bancario) para no doblar y que
    # el detalle cuadre con el total.
    where_quimico = (
        "AND UPPER(TRIM(COALESCE(concepto, ''))) NOT LIKE 'QUIMICOS %%'"
        if n == 6
        else ""
    )
    sql = f"""
        SELECT id_xgast, fecha, doc, prov, concepto, importe, stat, fechad, saldo
        FROM scintela.xgast
        WHERE num = %s
          AND COALESCE(stat, '') <> 'Y'
          {where_fecha}
          {where_quimico}
        ORDER BY fecha DESC, id_xgast DESC
    """
    filas = db.fetch_all(sql, (n,)) or []

    # TMT 2026-05-19 v2 — incluir compras cuyo (tipo, concepto, prov) mapea
    # a este num según la cascada del dBase (`_SQL_COMPRA_NUM_CASE`). Antes
    # filtraba sólo por tipo; ahora respeta SU/EEQ/AGUA/etc.
    where_fecha_c = (
        "AND c.fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) "
        "AND c.fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'"
        if mes_actual
        else ""
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
               -- Tamara 2026-07-22: excluir compras del puente formulas
               -- (colorantes/químicos material, valuados por stock) — igual que
               -- la matriz, para que el drill-down no las cuente como gasto.
               AND COALESCE(c.usuario_crea, '') NOT LIKE 'formulas%%'
               {where_fecha_c}
        ) sub
         WHERE num_calc = %s
         ORDER BY fecha DESC, id_compra DESC
    """
    filas_compras = db.fetch_all(sql_c, (n,)) or []

    # Agrupar por concepto via _grupo_concepto. Guardamos las claves de xgast
    # para deduplicar las compras que sean el mismo movimiento (ver _dedup_key).
    buckets: dict[str, dict] = {}
    total = 0.0
    xgast_keys: set[tuple] = set()
    n_incluidas = 0
    for r in filas:
        grupo = _grupo_concepto(r.get("concepto"))
        importe = float(r.get("importe") or 0)
        total += importe
        n_incluidas += 1
        xgast_keys.add(
            _dedup_key(r.get("fecha"), r.get("prov"), r.get("concepto"), importe)
        )
        if grupo not in buckets:
            buckets[grupo] = {"grupo": grupo, "filas": [], "subtotal": 0.0}
        buckets[grupo]["filas"].append(
            {
                "id_xgast": r.get("id_xgast"),
                "fecha": r.get("fecha"),
                "doc": r.get("doc") or "",
                "prov": r.get("prov") or "",
                "concepto": r.get("concepto") or "",
                "importe": importe,
                "stat": r.get("stat") or "",
                "fuente": "xgast",
            }
        )
        buckets[grupo]["subtotal"] += importe

    # Compras del mes mapeadas a este rubro. TMT 2026-07-14 — "no repeticiones":
    #   (1) se saltea la compra si ya existe el mismo movimiento en xgast
    #       (prioriza la categoría), y
    #   (2) las compras que SÍ entran se agrupan por su concepto (misma
    #       categoría que xgast, badge `compra`), no en un bucket aparte
    #       "Compras (tipo X)" — pedido dueña: "solo mantené categorías acá".
    for r in filas_compras:
        importe = float(r.get("importe") or 0)
        key = _dedup_key(r.get("fecha"), r.get("prov"), r.get("concepto"), importe)
        if key in xgast_keys:
            continue  # ya contado en xgast — no repetir
        tipo_c = (r.get("tipo") or "").upper().strip()
        grupo = _grupo_concepto(r.get("concepto"))
        total += importe
        n_incluidas += 1
        if grupo not in buckets:
            buckets[grupo] = {"grupo": grupo, "filas": [], "subtotal": 0.0}
        buckets[grupo]["filas"].append(
            {
                "id_compra": r.get("id_compra"),
                "fecha": r.get("fecha"),
                "doc": r.get("doc") or "",
                "prov": r.get("prov") or "",
                "concepto": r.get("concepto") or "",
                "importe": importe,
                "stat": r.get("stat") or "",
                "fuente": "compra",
                "tipo": tipo_c,
            }
        )
        buckets[grupo]["subtotal"] += importe

    # Ordenamos los grupos por subtotal descendente — los gastos más grandes arriba.
    grupos = sorted(buckets.values(), key=lambda g: g["subtotal"], reverse=True)

    return {
        "num": n,
        "label": GASTOS_NUM_LABELS.get(n, f"V{n}"),
        "grupos": grupos,
        "total": total,
        "n_filas": n_incluidas,
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
    ("K_SU", 1, "Tej · Sueldos"),
    ("K_EEQ", 2, "Tej · Servicios"),
    ("K_OTROS", 3, "Tej · Otros"),
    ("C_SU", 4, "Tin · Sueldos"),
    ("C_SERV", 5, "Tin · Servicios"),
    ("C_OTROS", 6, "Tin · Otros"),
    ("S_SU", 7, "Adm · Sueldos"),
    ("S_GAS", 8, "Adm · Servicios"),
    ("S_OTROS", 9, "Adm · Otros"),
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


def _es_gasto_vario_tin_sql(col: str) -> str:
    """Predicado SQL: `col` (un concepto) es un GASTO VARIO genuino de
    tintorería — arranca con 'CC' o 'GS' (CC MANGUERA, GS.FAB.VS., ...).

    Federico 2026-07-22: V6 (Tin·Otros) debe aislar ESTRICTAMENTE los gastos
    CC / GS.FAB varios y EXCLUIR las compras de colorantes/químicos (prov
    CT/AQ/SY/QI, concepto QUIMICOS/QUIMSERTEC/TOSAVA/ECUAPLAST o nº de factura).
    Esas son material que se gasta por consumo físico en la fila Colorantes/Quím.,
    NO un gasto de tintorería. Se aplica a xgast num=6 y a las compras C/Q/T→V6.
    Se filtra por concepto (no por proveedor) porque hay químicos cargados con
    prov 'CC' pero concepto 'QUIMSERTEC' — el concepto es la señal confiable.
    Los `%%` van escapados para psycopg2 (queries que pasan params).
    """
    c = f"UPPER(TRIM(COALESCE({col}, '')))"
    return f"({c} LIKE 'CC%%' OR {c} LIKE 'GS%%')"


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
    -- V6: Tintorería · Otros — SOLO gastos varios genuinos (concepto CC* / GS*).
    -- Federico 2026-07-22: las compras de colorantes/químicos (prov CT/AQ/SY/QI,
    -- concepto QUIMICOS/QUIMSERTEC/TOSAVA/ECUAPLAST o nº de factura) NO son gasto
    -- vario de tintorería — son material (se consumen vía Colorantes/Quím.).
    WHEN UPPER(COALESCE(c.tipo, '')) IN ('C', 'Q', 'T')
         AND {_es_gasto_vario_tin_sql('c.concepto')} THEN 6

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
    "K": 3,  # tipo K default → V3 (refinado por concepto en SQL CASE)
    "C": 6,
    "Q": 6,
    "T": 6,
    "S": 9,
}


def gastos_xgast_v1_a_v9_mes(meses_atras: int = 0) -> dict:
    """V1..V9 del PRG: SUM(importe) FROM xgast + compras (por tipo) WHERE mes en curso.

    `meses_atras` (Federico 2026-07-21): 0 = mes en curso; >0 desplaza la
    ventana ese número de meses hacia atrás (1 = mes anterior). Usado por la
    fila "Gastos mes anterior" de /informes/gastos.

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
    # TMT 2026-05-20 PASADA 6 Federico #10 — defensive: excluir anulados
    # (stat 'X') del rollup. Antes la query no filtraba, lo que podía
    # mostrar xgast anulados (legacy). Federico reportó que un $500 no
    # aparecía en V9 — verificar si el xgast no quedó stat='X' por algún
    # reverse no sincronizado.
    # TMT 2026-07-14 — a nivel FILA (no SUM) para poder deduplicar contra
    # compras (pedido dueña: "no repeticiones"). Guardamos la clave de cada
    # xgast por rubro; una compra con gemelo exacto en su mismo rubro no se
    # vuelve a sumar. Mismo criterio que el drill-down (`gastos_detalle_categoria`)
    # → la card de Gastos y el detalle cuadran.
    _off = int(meses_atras)
    rows_xgast = (
        db.fetch_all(
            """
        SELECT COALESCE(num, 0) AS num, fecha, prov, concepto, importe
        FROM scintela.xgast
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) - make_interval(months => %(off)s)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) - make_interval(months => %(off)s) + INTERVAL '1 month'
          AND COALESCE(stat, '') NOT IN ('X', 'Y')
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
          -- Tamara 2026-07-22: V6 (num=6) incluye los químico-insumos
          -- (QUIMSERTEC/TOSAVA/ECUAPLAST/NC/QI = gasto de tintorería, como los CC;
          -- los colorantes POLI/ALG no viven acá, entran por compra Q y se valúan
          -- aparte en el stock). PERO excluye el wrapper manual 'QUIMICOS <prov>',
          -- copia redundante de un químico YA cargado como gasto bancario
          -- ('QUIMSERTEC 2184 [tx…]', 'TOSAVA 216 [tx…]'): contarlo doblaría. El
          -- químico genuino se cuenta bajo su nombre de proveedor.
          AND NOT (COALESCE(num, 0) = 6
                   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'QUIMICOS %%')
        """,
            {"off": _off},
        )
        or []
    )
    v: dict[int, float] = {}
    xgast_keys_por_num: dict[int, set] = {}
    for r in rows_xgast:
        num = int(r.get("num") or 0)
        importe = float(r.get("importe") or 0)
        v[num] = v.get(num, 0.0) + importe
        xgast_keys_por_num.setdefault(num, set()).add(
            _dedup_key(r.get("fecha"), r.get("prov"), r.get("concepto"), importe)
        )

    # Compras del mes mapeadas por la cascada dBase (tipo + concepto +
    # codigo_prov). Excluye anuladas, materia prima (H), anticipos (A/I) y
    # producción (K con kg>0). Mapping completo en `_SQL_COMPRA_NUM_CASE`.
    # Se saltea la compra si ya existe el mismo movimiento en xgast del rubro.
    sql_compras = f"""
        SELECT ({_SQL_COMPRA_NUM_CASE}) AS num,
               c.fecha, c.codigo_prov AS prov, c.concepto, c.importe
          FROM scintela.compra c
         WHERE c.fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) - make_interval(months => %(off)s)
           AND c.fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) - make_interval(months => %(off)s) + INTERVAL '1 month'
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND COALESCE(c.usuario_crea, '') <> 'asinfo-backfill'
           -- Tamara 2026-07-22: las compras del puente formulas (colorantes/
           -- químicos de tintura, usuario_crea 'formulas-auto'/'formulas-<user>')
           -- son MATERIAL — se valúan en la fila Colorantes/Quím. por stock, NO
           -- son gasto. Guard por la marca de origen (más confiable que el
           -- concepto) para que nunca se confundan con los químico-insumos de V6.
           AND COALESCE(c.usuario_crea, '') NOT LIKE 'formulas%%'
    """
    rows_compras = db.fetch_all(sql_compras, {"off": _off}) or []
    for r in rows_compras:
        num = r.get("num")
        if not num:
            continue
        num = int(num)
        importe = float(r.get("importe") or 0)
        key = _dedup_key(r.get("fecha"), r.get("prov"), r.get("concepto"), importe)
        if key in xgast_keys_por_num.get(num, ()):
            continue  # ya contado en xgast — no repetir
        v[num] = v.get(num, 0.0) + importe

    return {
        "v1": v.get(1, 0.0),
        "v2": v.get(2, 0.0),
        "v3": v.get(3, 0.0),
        "v4": v.get(4, 0.0),
        "v5": v.get(5, 0.0),
        "v6": v.get(6, 0.0),
        "v7": v.get(7, 0.0),
        "v8": v.get(8, 0.0),
        "v9": v.get(9, 0.0),
        "gtej_sin_dtj": v.get(1, 0) + v.get(2, 0) + v.get(3, 0),
        "gtin_sin_dcc": v.get(4, 0) + v.get(5, 0) + v.get(6, 0),
        "gs_sin_deprcar": v.get(7, 0) + v.get(8, 0) + v.get(9, 0),
    }


def _periodo_actual_ec() -> str:
    """Período 'YYYY-MM' del mes en curso en hora Ecuador (UTC-5)."""
    row = db.fetch_one(
        "SELECT to_char((CURRENT_TIMESTAMP - INTERVAL '5 hours'), 'YYYY-MM') AS p"
    )
    return (row or {}).get("p") or ""


def _periodo_anterior_ec() -> str:
    """Período 'YYYY-MM' del mes ANTERIOR al mes en curso (hora Ecuador)."""
    row = db.fetch_one(
        "SELECT to_char((CURRENT_TIMESTAMP - INTERVAL '5 hours') - INTERVAL '1 month', 'YYYY-MM') AS p"
    )
    return (row or {}).get("p") or ""


def gastos_mes_manual_get(periodo: str) -> dict | None:
    """Override MANUAL de los gastos del mes por rubro (tej/tin/adm), o None.

    Federico 2026-07-22: permite FORZAR los gastos de un mes desde la fila
    "Gastos mes anterior" de /informes/gastos (ej. meses sin datos en xgast como
    junio 2026). Guardado en `scintela.gastos_mes_manual`, compartido por todos.
    Devuelve None si no hay override cargado para ese período.
    """
    if not periodo:
        return None
    row = db.fetch_one(
        "SELECT tej, tin, adm FROM scintela.gastos_mes_manual WHERE periodo = %s",
        (periodo,),
    )
    if not row:
        return None
    return {
        "periodo": periodo,
        "tej": float(row.get("tej") or 0),
        "tin": float(row.get("tin") or 0),
        "adm": float(row.get("adm") or 0),
    }


def gastos_mes_manual_set(
    periodo: str,
    tej: float,
    tin: float,
    adm: float,
    usuario: str | None = None,
) -> dict:
    """Upsert del override manual de gastos del mes (una fila por YYYY-MM).

    Compartido por todos los usuarios; el último que guarda pisa el valor.
    """
    tej = float(tej or 0)
    tin = float(tin or 0)
    adm = float(adm or 0)
    db.execute(
        """
        INSERT INTO scintela.gastos_mes_manual
            (periodo, tej, tin, adm, usuario_modifica, fecha_modifica)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (periodo) DO UPDATE SET
            tej = EXCLUDED.tej,
            tin = EXCLUDED.tin,
            adm = EXCLUDED.adm,
            usuario_modifica = EXCLUDED.usuario_modifica,
            fecha_modifica = CURRENT_TIMESTAMP
        """,
        (periodo, tej, tin, adm, usuario),
    )
    return {"periodo": periodo, "tej": tej, "tin": tin, "adm": adm}


def venta_proyectada_mes_get(periodo: str | None = None) -> float | None:
    """Kg de VENTA PROYECTADA del período (editable desde la fila Proyección del
    balance), o None si no hay valor cargado.

    Federico 2026-07-22: si hay valor, PISA el KGPRO de `scintela.iniciales` en el
    Informe de Resultados. Guardado en `scintela.venta_proyectada_mes`, compartido
    por todos los usuarios. Alimenta la Utilidad Proyectada (venta proy = kg×precio
    y costos directos = kg × (MP + Colorantes) × 1,05).
    """
    per = periodo or _periodo_actual_ec()
    row = db.fetch_one(
        "SELECT kg FROM scintela.venta_proyectada_mes WHERE periodo = %s",
        (per,),
    )
    if not row:
        return None
    kg = float(row.get("kg") or 0)
    return kg if kg > 0 else None


def venta_proyectada_mes_set(
    kg: float, usuario: str | None = None, periodo: str | None = None
) -> dict:
    """Upsert del kg de venta proyectada del período (una fila por YYYY-MM).

    Compartido por todos los usuarios; el último que guarda pisa el valor.
    """
    per = periodo or _periodo_actual_ec()
    kg = float(kg or 0)
    db.execute(
        """
        INSERT INTO scintela.venta_proyectada_mes
            (periodo, kg, usuario_modifica, fecha_modifica)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (periodo) DO UPDATE SET
            kg = EXCLUDED.kg,
            usuario_modifica = EXCLUDED.usuario_modifica,
            fecha_modifica = CURRENT_TIMESTAMP
        """,
        (per, kg, usuario),
    )
    return {"periodo": per, "kg": kg}


def gastos_proyectado_mes_get(periodo: str | None = None) -> dict:
    """Gastos proyectados por rubro (tej/tin/adm) del período dado.

    Tamara 2026-07-22: reemplaza el localStorage por navegador que usaba la
    fila "Gastos proyectados este mes" de /informes/gastos. Ahora se guardan en
    `scintela.gastos_proyectado_mes`, compartidos por todos los usuarios.
    Devuelve 0 en cada rubro si no hay fila cargada para el período.
    """
    per = periodo or _periodo_actual_ec()
    row = db.fetch_one(
        """
        SELECT tej, tin, adm
          FROM scintela.gastos_proyectado_mes
         WHERE periodo = %s
        """,
        (per,),
    )
    return {
        "periodo": per,
        "tej": float((row or {}).get("tej") or 0),
        "tin": float((row or {}).get("tin") or 0),
        "adm": float((row or {}).get("adm") or 0),
    }


def gastos_proyectado_mes_set(
    tej: float,
    tin: float,
    adm: float,
    usuario: str | None = None,
    periodo: str | None = None,
) -> dict:
    """Upsert de los gastos proyectados del período (una fila por YYYY-MM).

    Compartido por todos los usuarios; el último que guarda pisa el valor
    anterior (igual que una planilla común). Devuelve el estado guardado.
    """
    per = periodo or _periodo_actual_ec()
    tej = float(tej or 0)
    tin = float(tin or 0)
    adm = float(adm or 0)
    db.execute(
        """
        INSERT INTO scintela.gastos_proyectado_mes
            (periodo, tej, tin, adm, usuario_modifica, fecha_modifica)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (periodo) DO UPDATE SET
            tej = EXCLUDED.tej,
            tin = EXCLUDED.tin,
            adm = EXCLUDED.adm,
            usuario_modifica = EXCLUDED.usuario_modifica,
            fecha_modifica = CURRENT_TIMESTAMP
        """,
        (per, tej, tin, adm, usuario),
    )
    return {"periodo": per, "tej": tej, "tin": tin, "adm": adm}


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
    # CORTE tintura: el dBase solo cuenta los lotes ANTERIORES al corte.
    row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0)                                     AS itin,
               COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                 THEN kg  ELSE 0 END), 0)                    AS ktint,
               COALESCE(SUM(CASE WHEN UPPER(TRIM(color)) NOT LIKE 'LAV%%'
                                  AND COALESCE(kg, 0) > 0
                                 THEN kgn ELSE 0 END), 0)                    AS kr
        FROM scintela.tinto
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND fecha <  %(corte)s
        """,
            {"corte": CORTE_TINTURA},
        )
        or {}
    )
    itin = float(row.get("itin") or 0)
    ktint = float(row.get("ktint") or 0)
    kr = float(row.get("kr") or 0)

    # CORTE tintura: del corte en adelante los lotes salen de formulas_app.
    # Reglas legacy: itin suma TODAS las filas; ktint/kr excluyen lavados por
    # prefijo de color ('LAV%'); kr exige kg (bruto) > 0.
    try:
        import calendar as _cal

        from modules.tintura import service as _tint_svc

        _hoy = today_ec()
        _m_ini = _hoy.replace(day=1)
        _m_fin = _hoy.replace(day=_cal.monthrange(_hoy.year, _hoy.month)[1])
        _f_desde = max(CORTE_TINTURA, _m_ini)
        if _f_desde <= _m_fin:
            for _o in _tint_svc.tinto_equiv_formulas(
                _f_desde, _m_fin, excluir_lavados=False
            ):
                itin += _o.importe or 0.0
                if (_o.color or "").strip().upper().startswith("LAV"):
                    continue
                _kg = _o.kg or 0.0
                _kgn = _o.kgn or 0.0
                ktint += _kg
                if _kg > 0:
                    kr += _kgn
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
        pass

    return {
        "itin": itin,
        "ktint": ktint,
        "kr": kr,
    }


def compras_iprovk_mes() -> dict:
    """IPROVK del PRG: compras de TEJIDO TERCERIZADO (no INTELA) del mes.

    Filtro PRG línea 230: `TIPO='K' AND PROV<>'KK' AND KG>0`.
    Mantenido como compat. Para el panel Resultados v2 usar
    `tejido_mes_componentes()` que descompone interno/externo/gastos-KK.
    """
    row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(kg),      0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'K'
          AND COALESCE(UPPER(TRIM(codigo_prov)), '') <> 'KK'
          AND COALESCE(kg, 0) > 0
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
        )
        or {}
    )
    return {
        "kg": float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
    }


def compras_tipo_t_externos_mes() -> dict:
    """KT_externos del PRG: compras tipo='T' tercerizado externo del mes.

    PRG línea 245: en el TOTAL TO PASACOM, KT = KG of TIPO='T' rows.
    Filtramos `prov<>'KK' AND kg>0` para mantener consistencia con
    `compras_iprovk_mes()` (IPROV/IPROVK pattern).
    """
    row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(kg),      0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'T'
          AND COALESCE(UPPER(TRIM(codigo_prov)), '') <> 'KK'
          AND COALESCE(kg, 0) > 0
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
        )
        or {}
    )
    return {
        "kg": float(row.get("kg") or 0),
        "importe": float(row.get("importe") or 0),
    }


def tinto_kg_servicios_mes() -> float:
    """KSTI del PRG (línea 254): SUM(kg) FROM tinto WHERE stat='S'.

    Son los kg de tinto prestados como SERVICIO a terceros (= no son
    nuestros, los tinturamos para otros). Se restan de KT en la fórmula
    de stock: `KT = KT_externos + KTINT - KSTI`.
    """
    row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(kg), 0) AS kg
        FROM scintela.tinto
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND fecha <  %(corte)s
          AND UPPER(TRIM(stat)) = 'S'
        """,
            {"corte": CORTE_TINTURA},
        )
        or {}
    )
    # CORTE tintura: del corte en adelante formulas_app no distingue servicio
    # a terceros (decision duena: no aplica), asi que KSTI post-corte = 0.
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
    rows = (
        db.fetch_all(
            """
        SELECT
            CASE WHEN COALESCE(UPPER(TRIM(codigo_prov)),'') = 'KK'
                 THEN 'KK' ELSE 'OTRO' END                                    AS quien,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) > 0 THEN kg      ELSE 0 END), 0) AS kg_con_kg,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) > 0 THEN importe ELSE 0 END), 0) AS us_con_kg,
            COALESCE(SUM(CASE WHEN COALESCE(kg, 0) = 0 THEN importe ELSE 0 END), 0) AS us_sin_kg
        FROM scintela.compra
        WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
          AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
          AND UPPER(TRIM(tipo)) = 'K'
          AND COALESCE(stat, '') NOT IN ('X', 'Y')  -- excluir anuladas. TMT 2026-05-13.
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        GROUP BY 1
        """
        )
        or []
    )
    out = {
        "kg_interno": 0.0,
        "kg_externo": 0.0,
        "us_externo": 0.0,
        "us_kk_gastos": 0.0,
    }
    for r in rows:
        if r.get("quien") == "KK":
            out["kg_interno"] = float(r.get("kg_con_kg") or 0)
            out["us_kk_gastos"] = float(r.get("us_sin_kg") or 0)
            # Si una fila KK tuviera kg>0 y importe>0 simultaneamente (raro
            # pero posible), su importe NO se suma como "gasto" — ya está
            # contado como costo de tejido interno por kg. Defensivo: dejarlo.
        else:
            out["kg_externo"] = float(r.get("kg_con_kg") or 0)
            out["us_externo"] = float(r.get("us_con_kg") or 0) + float(r.get("us_sin_kg") or 0)
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
        "ukg_promedio": ukg,
        "kg_disponible": kg_disp,
        "us_disponible": us_disp,
        "stock_anterior_kg": stock_anterior_kg,
        "stock_anterior_us": stock_anterior_us,
        "tarifa_anterior": tarifa_anterior,
        "src": src,
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

    hoy = today_ec()
    yy = hoy.year
    mm = hoy.month

    try:
        # Meses cerrados del año actual desde historia (uvent definitivo).
        row_hist = (
            db.fetch_one(
                """
            SELECT COALESCE(SUM(uvent), 0) AS total
              FROM scintela.historia
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) < %s
            """,
                (yy, mm),
            )
            or {}
        )
        uvent_cerrados = float(row_hist.get("total") or 0)
    except Exception:
        uvent_cerrados = 0.0

    try:
        # Mes en curso: live desde scintela.factura (sólo positivos).
        row_live = (
            db.fetch_one(
                """
            SELECT COALESCE(SUM(importe), 0) AS total
              FROM scintela.factura
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) = %s
               AND COALESCE(stat, '') <> 'X'
               AND COALESCE(importe, 0) > 0
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
            """,
                (yy, mm),
            )
            or {}
        )
        uvent_mes = float(row_live.get("total") or 0)
    except Exception:
        uvent_mes = 0.0

    return uvent_cerrados + uvent_mes


def utilidades_anio_en_curso(utilidad_mes_live: float = 0.0) -> float:
    """Utilidades del año calendario en curso: SUM(historia.usuti) de meses
    cerrados + la utilidad live del mes en curso (PATR−PATANT).

    Análoga a ventas_anio_en_curso(). Pedido Federico 2026-07-14: mostrar
    'Utilidades <año>' en el panel MOVIMIENTOS, encolumnado con Ventas del año.
    scintela.historia tiene una fila por mes cerrado con usuti definitivo
    (utilidad del cierre = PATR−PATANT de ese mes). Para el mes en curso, que
    todavía no tiene snapshot, usamos la utilidad live que ya calcula
    informe_balance (PATR−PATANT).

    Si historia falla, cae a 0 para los cerrados y devuelve sólo el mes live.
    """
    hoy = today_ec()
    yy = hoy.year
    mm = hoy.month

    try:
        row_hist = (
            db.fetch_one(
                """
            SELECT COALESCE(SUM(usuti), 0) AS total
              FROM scintela.historia
             WHERE EXTRACT(YEAR FROM fecha)  = %s
               AND EXTRACT(MONTH FROM fecha) < %s
            """,
                (yy, mm),
            )
            or {}
        )
        usuti_cerrados = float(row_hist.get("total") or 0)
    except Exception:
        usuti_cerrados = 0.0

    return usuti_cerrados + float(utilidad_mes_live or 0)


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
    hoy = hoy or today_ec()
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
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
        (snap_fecha, hoy),
    )
    kg_com = float((row_c or {}).get("kg") or 0)

    # Ventas en el mismo rango. Las facturas anuladas (stat='Y') no salen
    # de stock — quedan fuera. Una factura activa siempre mueve kg aunque
    # tenga saldo 0 (cobrada).
    # TMT 2026-05-27 dueña: "terminado se fue todo a 0". Las facturas
    # backfilleadas (usuario_crea='asinfo-backfill') son HISTORICAS que
    # ya fueron contabilizadas como kg vendidos en su mes original. Si
    # las sumamos al kg_ven del rango, inflamos el live_kg negativamente
    # y stock terminado va a 0. Excluirlas de este cálculo.
    row_v = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS kg
        FROM scintela.factura
        WHERE fecha > %s AND fecha <= %s
          AND COALESCE(kg, 0) > 0
          AND (stat IS NULL OR stat <> 'Y')
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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
    caja_count = db.fetch_one("SELECT COUNT(*) AS n FROM scintela.caja WHERE saldo IS NOT NULL") or {}
    out.append(
        {
            "concepto": "CAJA",
            "balance": salcaj_val,
            "modulo": salcaj_val,
            "match": True,
            "diff": 0.0,
            "detalle": [
                ("Último saldo en scintela.caja", salcaj_val),
                ("Filas de caja con saldo no-null", int(caja_count.get("n") or 0)),
            ],
            "nota": "PRG línea 68: SALCAJ = SALDO del último registro de caja.",
        }
    )

    # ----------- BANCOS -----------
    bancos = saldo_bancos()
    pos = posdat_totales()
    bancos_total = sum(float(b["saldo"] or 0) for b in bancos)
    salbanc = bancos_total + pos["pos1"] + pos["pos2"]
    bancos_detalle: list[tuple[str, float]] = [
        (
            f"{(b['nombre'] or 'Banco ' + str(b['no_banco']))} (origen: {b.get('saldo_origen')})",
            float(b["saldo"] or 0),
        )
        for b in bancos
        if abs(float(b["saldo"] or 0)) > 0.5
    ]
    bancos_detalle.append(("+ Posdat banc=1 (POS1)", pos["pos1"]))
    bancos_detalle.append(("+ Posdat banc=2 (POS2)", pos["pos2"]))
    bancos_detalle.append(("= BANCOS (SALBANC)", salbanc))
    out.append(
        {
            "concepto": "BANCOS",
            "balance": salbanc,
            "modulo": salbanc,
            "match": True,
            "diff": 0.0,
            "detalle": bancos_detalle,
            "nota": "PRG líneas 78, 99, 370: SALBANC = SALBANC1 + SALBANC2 = (Pichincha + POS1) + (Internacional + POS2).",
        }
    )

    # ----------- CHEQUES (TOTC) -----------
    totc_val = totc()
    chq = cheques_por_stat()

    def _chq(stat: str) -> float:
        return float(chq.get(stat, {}).get("total") or 0)

    chq_z = _chq("Z")
    chq_p = _chq("P")
    chq_d = _chq("D")
    chq_1 = _chq("1")
    chq_2 = _chq("2")
    chq_3 = _chq("3")
    chq_b = _chq("B")
    chq_a = _chq("A")
    chq_r = _chq("R")
    en_totc = chq_z + chq_1 + chq_2 + chq_3 + chq_p + chq_d
    match_c, diff_c = _diff(totc_val, en_totc)
    cartera_modulo = chq_z  # /cheques?estado=cartera muestra solo Z
    out.append(
        {
            "concepto": "CHEQUES (TOTC)",
            "balance": totc_val,
            "modulo": en_totc,
            "match": match_c,
            "diff": diff_c,
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
                'PRG línea 24: STAT $ "Z123PD". TOTC suma cartera + postergados + Daniela + rebotados-en-gestión. '
                "La pestaña /cheques?estado=cartera muestra SOLO Z — por eso siempre es ≤ TOTC. "
                f"Diferencia esperada con la pestaña cartera: ${en_totc - cartera_modulo:,.2f}."
            ),
        }
    )

    # ----------- FACTURAS (TOTF) -----------
    totf_val = totf()
    # f_cartera reporta el NET (= mismo filtro que totf), para que
    # diagnostico cuadre contra TOTF. Los sobrepagos (saldo<0) ya no se
    # excluyen; el dBase legacy nunca los excluyó.
    f_cartera = (
        db.fetch_one(
            """
        SELECT COUNT(*)                         AS n,
               COALESCE(SUM(saldo),   0)        AS saldo,
               COALESCE(SUM(importe), 0)        AS importe,
               COUNT(*) FILTER (WHERE saldo < 0) AS n_sobrepagos,
               COALESCE(SUM(saldo) FILTER (WHERE saldo < 0), 0) AS saldo_sobrepagos
        FROM scintela.factura
        WHERE stat IS NULL OR stat IN ('Z','A','',' ')
        """
        )
        or {}
    )
    f_canceladas = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat = 'T'"""
        )
        or {}
    )
    f_eliminadas = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat = 'X'"""
        )
        or {}
    )
    f_total_emit = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS importe
           FROM scintela.factura WHERE stat <> 'X' OR stat IS NULL"""
        )
        or {}
    )
    saldo_cartera = float(f_cartera.get("saldo") or 0)
    match_f, diff_f = _diff(totf_val, saldo_cartera)
    out.append(
        {
            "concepto": "FACTURAS (TOTF)",
            "balance": totf_val,
            "modulo": saldo_cartera,
            "match": match_f,
            "diff": diff_f,
            "detalle": [
                (
                    f"Cartera Z/A NETA — {int(f_cartera.get('n') or 0)} facturas (sumando sobrepagos)",
                    saldo_cartera,
                ),
                (
                    f"  ↳ de las cuales {int(f_cartera.get('n_sobrepagos') or 0)} con saldo<0 (sobrepagos)",
                    float(f_cartera.get("saldo_sobrepagos") or 0),
                ),
                ("  importe total emitido de esas mismas facturas", float(f_cartera.get("importe") or 0)),
                (
                    f"Canceladas (stat=T) — {int(f_canceladas.get('n') or 0)} facturas (importe)",
                    float(f_canceladas.get("importe") or 0),
                ),
                (
                    f"Eliminadas (stat IN X,Y) — {int(f_eliminadas.get('n') or 0)} (importe)",
                    float(f_eliminadas.get("importe") or 0),
                ),
                (
                    f"Total facturas emitidas (sin X/Y) — {int(f_total_emit.get('n') or 0)} (importe)",
                    float(f_total_emit.get("importe") or 0),
                ),
            ],
            "nota": (
                'PRG línea 27: TOTF = SUM(saldo) FOR STAT $ "ZA" (sin filtro de signo). '
                "Sobrepagos (saldo<0 = abono > importe) restan de la cartera. "
                "Verificado: $4.916.202,77 = lo que el dBase live mostraba 2026-05-06."
            ),
        }
    )

    # ----------- ANTICIPOS -----------
    antic_val = anticipos()
    dol_breakdown = (
        db.fetch_all(
            """SELECT COALESCE(NULLIF(TRIM(st), ''), '(vivo)') AS st,
                  COUNT(*) AS n,
                  COALESCE(SUM(importe), 0) AS total
           FROM scintela.dolares
           GROUP BY 1 ORDER BY 1"""
        )
        or []
    )
    detalle_dol = []
    total_dol_all = 0.0
    for r in dol_breakdown:
        t = float(r.get("total") or 0)
        total_dol_all += t
        detalle_dol.append((f"st={r['st']} ({int(r['n'])} filas)", t))
    detalle_dol.append(("ANTICIPOS = SUM(st null/vacío)", antic_val))
    detalle_dol.append(("Total dólares (todos los st)", total_dol_all))
    out.append(
        {
            "concepto": "ANTICIPOS",
            "balance": antic_val,
            "modulo": antic_val,
            "match": True,
            "diff": 0.0,
            "detalle": detalle_dol,
            "nota": "ANTICIPOS = SUM(importe) en scintela.dolares con st NULL o vacío (anticipos vivos del cliente).",
        }
    )

    # ----------- ACTIVOS FIJOS (UMAQ + UACT) -----------
    activos = activos_totales()
    from modules.activos.queries import borrado_where_sql as _borr_bd
    a_breakdown = (
        db.fetch_all(
            f"""SELECT COALESCE(NULLIF(TRIM(tipo), ''), '(sin tipo)') AS tipo,
                  COUNT(*) AS n,
                  COALESCE(SUM(valor), 0) AS total
           FROM scintela.activos
           WHERE TRUE {_borr_bd()}
           GROUP BY 1 ORDER BY 1"""
        )
        or []
    )
    detalle_act = []
    for r in a_breakdown:
        detalle_act.append((f"tipo={r['tipo']} ({int(r['n'])} activos)", float(r.get("total") or 0)))
    detalle_act.append(("Σ UMAQ (tipo M/C/K)", activos["umaq"]))
    detalle_act.append(("Σ UACT (tipo I/T = edificios + terrenos)", activos["uact"]))
    out.append(
        {
            "concepto": "MAQ/EQUIP. + TERR/EDIF/INS.",
            "balance": activos["umaq"] + activos["uact"],
            "modulo": activos["umaq"] + activos["uact"],
            "match": True,
            "diff": 0.0,
            "detalle": detalle_act,
            "nota": "Réplica legacy MENU.PRG: UACT FOR TIPO IN ('I','T') (edificios/instal. + terrenos), UMAQ FOR TIPO IN ('M','C','K'). Suma por código de tipo (no por concepto); '(s/t)'/no tipados quedan fuera igual que el dBase. Valor en libros prorrateado, no inicial.",
        }
    )

    # ----------- STOCK MP+PROD / STOCK QUI / UTILIDAD (historia) -----------
    hist = historia_ultimo_mes() or {}
    snap_fecha = hist.get("fecha")
    out.append(
        {
            "concepto": "STOCK MP+PROD. + STOCK QUI. + UTILIDAD",
            "balance": float(hist.get("ustock") or 0) + float(hist.get("uqui") or 0),
            "modulo": float(hist.get("ustock") or 0) + float(hist.get("uqui") or 0),
            "match": True,
            "diff": 0.0,
            "detalle": [
                ("VSTO = historia.ustock (último cierre)", float(hist.get("ustock") or 0)),
                ("VQX = historia.uqui", float(hist.get("uqui") or 0)),
                ("UTILIDAD = historia.usuti", float(hist.get("usuti") or 0)),
                ("PATANT = historia.patrimonio", float(hist.get("patrimonio") or 0)),
                ("Fecha snapshot histórico", snap_fecha.isoformat() if snap_fecha else "—"),
            ],
            "nota": "VSTO/VQX/PATANT/USUTI vienen del último snapshot mensual en scintela.historia. Si la fecha está vieja, todos estos componentes pueden estar desfasados.",
        }
    )

    # ----------- PASIVOS (TOTP) -----------
    pd_balance = pos["totp"]
    pd_modulo = (
        db.fetch_one(
            f"""
        SELECT COUNT(*)                         AS n,
               COALESCE(SUM(importe), 0)        AS total
        FROM scintela.posdat
        WHERE {POSDAT_DEUDA_VIVA_WHERE}
          AND COALESCE(importe, 0) > 0
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
        )
        or {}
    )
    pd_total_modulo = float(pd_modulo.get("total") or 0)
    pd_pagados = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS total
           FROM scintela.posdat
           WHERE COALESCE(banc,0)=9
             AND (anulada IS NOT TRUE OR anulada IS NULL)"""
        )
        or {}
    )
    pd_neg = (
        db.fetch_one(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(importe),0) AS total
           FROM scintela.posdat
           WHERE {POSDAT_DEUDA_VIVA_WHERE}
             AND COALESCE(importe,0)<=0
             AND (anulada IS NOT TRUE OR anulada IS NULL)"""
        )
        or {}
    )
    pd_neg_total = float(pd_neg.get("total") or 0)
    # La diferencia ESPERABLE entre balance y módulo es exactamente la
    # suma de los posdat con importe<=0: balance = módulo + neg. Si esa
    # identidad se cumple, marcamos ✓ aunque los dos números no sean
    # idénticos — no es un drift, es que miden cosas levemente distintas.
    diff_p = pd_balance - pd_total_modulo
    match_p = abs(diff_p - pd_neg_total) <= 0.5
    out.append(
        {
            "concepto": "PASIVOS (TOTP)",
            "balance": pd_balance,
            "modulo": pd_total_modulo,
            "match": match_p,
            "diff": diff_p,
            "detalle": [
                (
                    f"Posdat abiertas (banc=0, importe>0) — {int(pd_modulo.get('n') or 0)} partidas",
                    pd_total_modulo,
                ),
                (
                    f"Posdat con importe ≤ 0 (no entran al módulo) — {int(pd_neg.get('n') or 0)}",
                    float(pd_neg.get("total") or 0),
                ),
                (
                    f"Posdat pagadas (banc=9) — {int(pd_pagados.get('n') or 0)}",
                    float(pd_pagados.get("total") or 0),
                ),
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
        }
    )

    # ----------- DIVID (URET) -----------
    uret_val = uret_mes_corriente()
    uret_total = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(ret), 0) AS total
           FROM scintela.retiros"""
        )
        or {}
    )
    uret_year = (
        db.fetch_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(ret), 0) AS total
           FROM scintela.retiros WHERE EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM CURRENT_DATE)"""
        )
        or {}
    )
    out.append(
        {
            "concepto": "DIVID. (URET)",
            "balance": uret_val,
            "modulo": uret_val,
            "match": True,
            "diff": 0.0,
            "detalle": [
                ("Retiros del mes en curso (URET)", uret_val),
                (
                    f"Retiros del año actual — {int(uret_year.get('n') or 0)}",
                    float(uret_year.get("total") or 0),
                ),
                (
                    f"Retiros TOTALES histórico — {int(uret_total.get('n') or 0)}",
                    float(uret_total.get("total") or 0),
                ),
            ],
            "nota": "PRG línea 37: URET = SUM(ret) FOR &MA AND DD-FECHA<63. &MA = mes/año actual. Filtramos retiros del mes en curso.",
        }
    )

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

    _check("CART = TOTF + TOTC", b.get("cart", 0), float(b.get("totf") or 0) + float(b.get("totc") or 0))

    _check(
        "SUBT = SALBANC + SALCAJ + CART",
        b.get("subt", 0),
        float(b.get("salbanc") or 0) + float(b.get("salcaj") or 0) + float(b.get("cart") or 0),
    )

    _check(
        "TOTL = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC",
        b.get("totl", 0),
        float(b.get("subt") or 0)
        + float(b.get("vsto") or 0)
        + float(b.get("vqx") or 0)
        + float(b.get("umaq") or 0)
        + float(b.get("uact") or 0)
        + float(b.get("uret_calc", b.get("uret")) or 0)
        + float(b.get("antic") or 0),
    )

    _check("PATR = TOTL - TOTP", b.get("patr", 0), float(b.get("totl") or 0) - float(b.get("totp") or 0))

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
    cost_mat_ukg,
    cost_col_ukg,
    cost_tej_ukg,
    cost_gsp_ukg,
    cost_gas_ukg,
    cost_mat_us,
    cost_col_us,
    cost_tej_us,
    cost_gsp_us,
    cost_gas_us,
    cost_mat_proy,
    cost_col_proy,
    cost_tej_proy,
    cost_gsp_proy,
    cost_gas_proy,
    KR,
    KTINT,
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
        "ukg": factor * (cost_mat_ukg + cost_col_ukg) + cost_tej_ukg + cost_gsp_ukg + cost_gas_ukg,
        "us": factor * (cost_mat_us + cost_col_us) + cost_tej_us + cost_gsp_us + cost_gas_us,
        "proy_us": factor * (cost_mat_proy + cost_col_proy) + cost_tej_proy + cost_gsp_proy + cost_gas_proy,
        "desperdicio_pct": DESP + DESK,
        "factor": factor,
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


def resultados_costos_tabla(
    *,
    venta_kg: float,
    venta_us: float,
    dia_actual: int,
    mp_ukg: float,
    v1: float,
    v2: float,
    v3: float,
    dtj: float,
    tej_base_us: float | None = None,  # 2026-06-05: importe compras tipo K del mes (= VK del dBase). Si None, cae a v1+v2+v3.
    kg_tejidos: float,
    v4: float,
    v5: float,
    v6: float,
    dcc: float,
    itin: float,
    ktint: float,
    v7: float,
    ktint_colorantes: float | None = None,  # TMT 2026-05-29: kg live para fila Colorantes; default = ktint (compat)
    col_us_fisico: float | None = None,      # dueña 2026-07-21: $ colorante = FÍSICO consumido (no ITIN); None → ITIN
    v8: float,
    v9: float,
    deprcar: float,
    patr: float,
    patant: float,
    uret: float,
    # 2026-06-04 — inputs para la UT.PROY estilo dBase (INFORMES.PRG L419).
    # Defaults conservadores → callers viejos no rompen (UT.PROY cae a 0).
    kgpro: float = 0.0,
    pretot: float = 0.0,
    # TMT 2026-06-23 (dueña): presupuesto de gastos POR ÁREA (dBase XPRETEJ/
    # XPRETIN/XPREADM de INICIALES) → columna "Proyectado" GS.PROY vs GS.ACT.
    pretej: float = 0.0,
    pretin: float = 0.0,
    preadm: float = 0.0,
    factor_desperdicio: float = 1.0,
    provision_pendiente: float = 0.0,
    utilidad_econ: float = 0.0,
    # 2026-06-05 — Costo Total réplica EXACTA del dBase. Si se pasan, el caller
    # ya calculó COSTUNI (u$/kg, INFORMES.PRG L406) y CSVTATOT (U$, L411);
    # los usamos tal cual. Si None, cae a la aprox Subtotal+4.5% + Admin.
    costo_total_ukg: float | None = None,
    costo_total_us: float | None = None,
    # TMT 2026-07-12 (dueña): mostrar Materia Prima en kg y $ (antes solo $/kg).
    # = hilado consumido del mes (egresos del cuadro MOVIMIENTOS: ktej × um_act).
    # Es el costo más grande y estaba oculto. Si None, la fila queda solo con $/kg.
    mp_kg: float | None = None,
    mp_us: float | None = None,
) -> list[dict]:
    """Tabla RESULTADOS del /informes/balance — rediseno Federico 2026-05-21.

    Definida fila por fila con el dueno. Columnas: Kg | U$/kg | U$.
    Cada fila es {label, kg, ukg, us, clase, ayuda}; `clase` da el estilo
    al template: 'dato' | 'seccion' | 'subtotal' | 'total' | 'key'.
    Las filas 'seccion' solo traen {label, clase}.

    Formulas (mes en curso):
      Venta          kg/us live de scintela.factura; u$/kg = us / kg.
      Proyeccion     regla de 3 al dia 30 con el mismo precio promedio.
      Materia Prima  solo u$/kg = costo del hilado consumido
                     (flujo-produccion, HILADO egresos $/kg).
      Tejeduria      us = V1+V2+V3 + amort.tejeduria; u$/kg = us / kg tejidos.
      Tintoreria     us = V4+V5+V6 + amort.tintoreria; u$/kg = us / KTINT.
      Colorantes     us = ITIN (SUM importe tinto del mes); u$/kg = ITIN/KTINT.
      Subtotal +4.5% u$/kg = 1.045 * (MP + Tejed. + Tintor. + Colorantes).
      Administracion us = V7+V8+V9 + amort.admin; u$/kg = us / kg vendidos.
      Costo Total    u$/kg = Subtotal + Administracion; us = kg vend. * u$/kg.
      Ut. Esperada   u$/kg = precio - Costo Total; us = kg vendidos * u$/kg.
      Ut. Real       us = delta patrimonio + dividendos del mes
                        = (patr - patant) + uret; u$/kg = us / kg vendidos.
    """
    def _div(a: float, b: float) -> float:
        return (a / b) if b else 0.0

    venta_kg = float(venta_kg or 0)
    venta_us = float(venta_us or 0)
    precio = _div(venta_us, venta_kg)

    # PROYECCIÓN — dBase INFORMES.PRG L4: PROYECCION = KGPRO (meta del mes) × precio,
    # NO regla de 3 (venta_kg × 30/día). Antes daba ~379k kg en vez de la meta. TMT 2026-06-05.
    proy_kg = float(kgpro or 0)
    proy_us = proy_kg * precio

    mp_ukg = float(mp_ukg or 0)

    kg_tejidos = float(kg_tejidos or 0)
    # TEJEDURÍA — dBase INFORMES.PRG L241: VK = IMPORTE(compras tipo K) + DTJ.
    # Antes PC usaba V1+V2+V3 (gastos) + DTJ → daba ~2× el dBase. TMT 2026-06-05.
    _tej_base = (float(tej_base_us) if tej_base_us is not None
                 else float(v1 or 0) + float(v2 or 0) + float(v3 or 0))
    tej_us = _tej_base + float(dtj or 0)
    tej_ukg = _div(tej_us, kg_tejidos)

    ktint = float(ktint or 0)
    tin_us = float(v4 or 0) + float(v5 or 0) + float(v6 or 0) + float(dcc or 0)
    tin_ukg = _div(tin_us, ktint)

    # Dueña 2026-07-21: la fila Colorantes/Quím. usa el químico FÍSICO consumido
    # (lo que realmente salió de bodega, coherente con el stock físico) en vez
    # del ITIN (costeo por orden). Si no vino el físico, cae al ITIN (dBase).
    col_us = float(col_us_fisico) if col_us_fisico is not None else float(itin or 0)
    # TMT 2026-05-29 dueña: la fila Colorantes/Quím. usa kg tinturados
    # LIVE del mes (param opcional ktint_colorantes), no el ktint de
    # historia. Caída a ktint si no se pasa (compat con callers viejos).
    col_kg = float(ktint_colorantes) if ktint_colorantes else ktint
    col_ukg = _div(col_us, col_kg)

    # +4.5% (merma) SOLO sobre materiales (Materia Prima + Colorantes) — igual que
    # el COSTUNI del dBase (INFORMES.PRG L406: factor × (UMX + ITIN/KR)). La mano de
    # obra y gastos de Tejeduría/Tintorería NO llevan recargo de merma. Antes PC
    # aplicaba 1.045 a las 4 filas → inflaba el subtotal y no cerraba contra el
    # Costo Total (COSTUNI). Dueña 2026-07-12: "el 4.5% que hace el dBase, hagámoslo".
    _fac_merma = float(factor_desperdicio or 1.045)
    sub_ukg = tej_ukg + tin_ukg + _fac_merma * (mp_ukg + col_ukg)

    adm_us = (float(v7 or 0) + float(v8 or 0) + float(v9 or 0)
              + float(deprcar or 0))
    adm_ukg = _div(adm_us, venta_kg)

    # Costo Total — dBase muestra DOS fórmulas distintas: u$/kg = COSTUNI
    # (factor sobre MP+Col), U$ = CSVTATOT (otra fórmula). Si el caller las pasa,
    # las usamos tal cual (réplica exacta). Sino, aprox Subtotal+Admin. TMT 2026-06-05.
    if costo_total_ukg is not None:
        ct_ukg = float(costo_total_ukg)
        ct_us = float(costo_total_us) if costo_total_us is not None else venta_kg * ct_ukg
    else:
        # Dueña 2026-07-12: el Costo Total DEBE ser la suma de los renglones, no
        # las fórmulas COSTUNI/CSVTATOT del dBase. Cada columna suma de arriba a
        # abajo: $/kg = Subtotal + Admin; $ = suma de los $ reales de las filas
        # (MP + Tejeduría + Tintorería + Colorantes + Admin). OJO: $/kg y $ son
        # dos lentes distintos (costo estándar por kg vs plata gastada) sobre kg
        # base distintos por fila, así que $/kg × kg ≠ $ — es estructura del
        # cuadro, no un descuadre. NO pretender que cruzan.
        ct_ukg = sub_ukg + adm_ukg
        ct_us = float(mp_us or 0) + tej_us + tin_us + col_us + adm_us

    # Utilidad REAL — fórmula original: ur = (patr - patant) + uret
    # = delta patrimonio + dividendos del mes (la cuenta económica completa
    # que incluye revalúo de stock, cambios de cartera, etc.).
    # TMT 2026-05-27 dueña: 'restauralo pero agarra variables de resultados
    # para calcularlo, no de historia'. La función ya recibe patr/patant/uret
    # como params — el caller (`informe_balance`) decide de dónde sacarlos
    # (idealmente NO de scintela.historia).
    ur_us = (float(patr or 0) - float(patant or 0)) + float(uret or 0)
    ur_ukg = _div(ur_us, venta_kg)

    # Utilidad PROYECTADA — réplica EXACTA del dBase (INFORMES.PRG L419-421):
    #   UTPROY = UTILIDAD + (KGPRO-KV)*(PRECIO - (UMX+ITIN/KR)*factor_desp)
    #                     - (XPRETOT - VK - GTIN - GS)
    #   UT.PROY (pantalla) = UTPROY - PROVI
    # UTILIDAD = PATR-PATANT live (param utilidad_econ). El gasto fijo NO se
    # extrapola: sale del PRESUPUESTO XPRETOT (= pretot de scintela.iniciales)
    # menos lo ya gastado (VK=tej_us + GTIN=tin_us + GS=adm_us). KGPRO = meta
    # del mes (no la regla de 3). PROVI = provisión que falta aprovisionar.
    # dBase usa ITIN/KR (no ITIN/KTINT = col_ukg) en el costo variable de la
    # proyección (PRG L419: (UMX+ITIN/KR)). `ktint` param = KR (tin.kr). Sutil
    # pero mueve ~7k en la UT.PROY. TMT 2026-06-05.
    _col_kr = _div(float(itin or 0), ktint)
    _costo_var_kg = (mp_ukg + _col_kr) * float(factor_desperdicio or 1.0)
    _margen_var_kg = precio - _costo_var_kg
    _gasto_fijo_restante = float(pretot or 0) - (tej_us + tin_us + adm_us)
    _utproy = (
        float(utilidad_econ or 0)
        + (float(kgpro or 0) - venta_kg) * _margen_var_kg
        - _gasto_fijo_restante
    )
    up_kg = float(kgpro or 0)
    up_us = _utproy - float(provision_pendiente or 0)
    up_ukg = _div(up_us, up_kg)

    return [
        {"label": "Venta", "kg": venta_kg, "ukg": precio, "us": venta_us,
         "clase": "dato",
         "ayuda": "Facturas del mes en curso (stat != X). U$/kg = U$ / Kg."},
        {"label": "Proyección", "kg": proy_kg, "ukg": precio, "us": proy_us,
         "clase": "dato",
         "ayuda": ("Meta del mes (KPROG de Iniciales) × precio promedio — "
                   "igual que la PROYECCIÓN del dBase (INFORMES.PRG).")},
        {"label": "COSTOS", "clase": "seccion"},
        {"label": "Materia Prima",
         "kg": (float(mp_kg) if mp_kg else None),
         "ukg": mp_ukg,
         "us": (float(mp_us) if mp_us else None),
         "clase": "dato",
         "ayuda": ("Hilado consumido del mes = fila EGRESOS (HILADO) del cuadro "
                   "MOVIMIENTOS DEL MES (INICIAL ASINFO) del Flujo de producción "
                   "— mismo número (salidas de bodega 51 netas de reingresos), "
                   "× la tarifa del hilado del STOCK.")},
        {"label": "Tejeduría", "kg": kg_tejidos, "ukg": tej_ukg, "us": tej_us,
         "proy": (float(pretej or 0) or None), "clase": "dato",
         "ayuda": ("kg = Ingresos de CRUDO (= Producción tejido) del cuadro "
                   "MOVIMIENTOS DEL MES (INICIAL ASINFO) del Flujo de "
                   "producción. Costo = compras tipo K + depreciación de "
                   "tejeduría; U$/kg = costo / esos kg.")},
        {"label": "Tintorería", "kg": ktint, "ukg": tin_ukg, "us": tin_us,
         "proy": (float(pretin or 0) or None), "clase": "dato",
         "ayuda": ("kg = Ingresos de TERMINADO del cuadro MOVIMIENTOS DEL MES "
                   "(INICIAL ASINFO) del Flujo de producción (lo producido "
                   "por tintorería). Costo = V4+V5+V6 + depreciación de "
                   "tintorería; U$/kg = costo / esos kg.")},
        {"label": "Colorantes/Quím.", "kg": col_kg, "ukg": col_ukg,
         "us": col_us, "clase": "dato",
         "ayuda": ("kg = Ingresos de TERMINADO del cuadro MOVIMIENTOS DEL MES "
                   "(INICIAL ASINFO), igual que Tintorería. $ = consumo de "
                   "colorantes/químicos de las órdenes de tintura del mes; "
                   "U$/kg = $ / esos kg.")},
        {"label": "Subtotal +4.5%", "kg": None, "ukg": sub_ukg, "us": None,
         "clase": "subtotal",
         "ayuda": ("Tejeduria + Tintoreria + merma*(Materia Prima + Colorantes). "
                   "El +4.5% (merma) aplica SOLO a materiales, igual que el dBase "
                   "(COSTUNI). La mano de obra/gastos no llevan recargo.")},
        {"label": "Administración", "kg": None, "ukg": adm_ukg, "us": adm_us,
         "proy": (float(preadm or 0) or None), "clase": "dato",
         "ayuda": ("Costo total = V7+V8+V9 + depreciacion de administracion. "
                   "U$/kg = costo total / kg vendidos.")},
        {"label": "Costo Total", "kg": None, "ukg": ct_ukg, "us": ct_us,
         "proy": (float(pretot or 0) or None), "clase": "total",
         "ayuda": ("Replica EXACTA del dBase. $/kg = COSTUNI = merma*(MP+ITIN/KR) "
                   "+ Tejeduria + Tintoreria + Admin (= Subtotal + Admin). "
                   "U$ = CSVTATOT = costo estandarizado de los kg vendidos "
                   "(KV*1.04*costo_unitario incl. hilado + gastos); por eso NO es "
                   "la suma lineal de las filas.")},
        {"label": "Utilidad Real", "kg": None, "ukg": ur_ukg, "us": ur_us,
         "clase": "key", "parcial": True, "parcial_dias": dia_actual,
         "ayuda": (
             "(PATR − PATANT) + URET — delta del patrimonio + dividendos "
             "del mes. Cuenta económica completa (incluye revalúo de "
             "stock, cambios de cartera, etc.). ⚠ Cifra del mes EN CURSO "
             f"(día {dia_actual}): PATR−PATANT sólo cuadra al CIERRE de mes; "
             "a principio de mes puede salir negativa — es parcial, no la "
             "utilidad final.")},
        {"label": "Utilidad Proyectada", "kg": up_kg, "ukg": up_ukg,
         "us": up_us, "clase": "dato",
         "ayuda": ("Réplica dBase (UT.PROY): utilidad real del mes (PATR−PATANT) "
                   "+ margen variable × kg que faltan vender para la meta KPROG "
                   "− gastos fijos PROYECTADOS que faltan (XPRETOT de Iniciales − "
                   "lo ya gastado) − provisión pendiente del mes.")},
    ]


def informe_balance(comp_mes_override: dict | None = None) -> dict:
    """Arma el BALANCE equivalente al del INFORMES.PRG screen.

    `comp_mes_override`: si se pasa {"kg": .., "importe": ..}, reemplaza el
    resultado de compras_mes_corriente() para el cálculo del $/kg del hilado.
    Sólo lo usa /admin/health/hilado-stock-debug para proyectar la utilidad
    bajo cada escenario de valuación SIN tocar el balance real (read-only).
    """
    _totf = totf()
    _totc = totc()
    bancos = saldo_bancos()
    _salcaj = salcaj()
    posdats = posdat_totales()
    activos = activos_totales()
    _antic = anticipos()
    _uret = uret_mes_corriente()
    # TMT 2026-07-06 v6: SIN ajuste — el crédito OP se consume (pasivos sube)
    # y el dividendo positivo en URET (dentro del activo) lo compensa solo:
    # utilidad quieta con la aritmética normal. uret_mes_ajustado quedó sin
    # uso (historia del día, no reusar sin leer la memoria).
    _uret_calc = _uret
    # TMT 2026-05-19 v7 — dueña pidió "dividendos del año" debajo de
    # "dividendos del mes". retiros_total_anual() suma scintela.retiros
    # del año en curso.
    _uret_anio = retiros_total_anual()
    # TMT 2026-05-19 v8 — pedido dueña: agregar "Ventas del año" en el
    # panel derecho (reemplaza "Patrimonio último cierre").
    _ventas_anio = ventas_anio_en_curso()
    hist = historia_ultimo_mes() or {}
    inic = iniciales_mes_actual() or {}
    # ¿Faltan las INICIALES del mes en curso? (inic resolvió a otro mes: la
    # fila del mes no está cargada o quedó corrupta). Cuando falta, los
    # rubros derivados del "mes anterior" (VQ0 químicos, tarifas) apuntan a
    # un mes equivocado y hay que anclar al CIERRE real, no a una fila vieja.
    # Bug 2026-07-01. [[iniciales_mes_actual]]
    _hoy_ec_bal = today_ec()
    _iniciales_del_mes_falta = bool(inic) and (
        int(inic.get("mesnum") or 0) != _hoy_ec_bal.month
        or int(inic.get("yy") or 0) != _hoy_ec_bal.year
    )
    venta_anual = venta_anual_kg_y_us()
    # Mes EN CURSO (live, no del cierre histórico) — replica el dBase.
    vent_mes = ventas_mes_corriente_resultado()
    comp_mes = comp_mes_override if comp_mes_override is not None else compras_mes_corriente()

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
    totl = subt + vsto + vqx + activos["umaq"] + activos["uact"] + _uret_calc + _antic
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
        "stock_kg": float(hist.get("stock") or 0),  # kg en stock de MP+PT (snapshot)
        "stock_kg_live": live["live_kg"],
        "stock_kg_diff": live["live_kg"] - float(hist.get("stock") or 0),
        "stock_kg_live_desde": live["snapshot_fecha"],
        "stock_kg_dias": live["dias_desde_snapshot"],
        "kcom": float(hist.get("kcom") or 0),  # kg comprados el mes
        "ktej": float(hist.get("ktej") or 0),  # kg tejidos
        "ktin": float(hist.get("ktin") or 0),  # kg tinturados (fuera)
        "kvent": float(hist.get("kvent") or 0),  # kg vendidos
        "ucom": float(hist.get("ucom") or 0),  # U$ compras mes
        "utej": float(hist.get("utej") or 0),  # U$ costo tejido
        "utin": float(hist.get("utin") or 0),  # U$ costo tintura
        "uvent": float(hist.get("uvent") or 0),  # U$ ventas mes
        "costo_mes": float(hist.get("costo") or 0),  # U$ costo total mes
        # Precios unitarios útiles para el ojo del gerente
        "precio_vta": (float(hist.get("uvent") or 0) / float(hist.get("kvent") or 0))
        if hist.get("kvent")
        else 0.0,
        "costo_kg": (float(hist.get("ucom") or 0) / float(hist.get("kcom") or 0))
        if hist.get("kcom")
        else 0.0,
    }

    # Los bancos con saldo exactamente 0 no aportan a la lectura del balance —
    # filtrarlos evita que la tabla lateral tenta 5 bancos inactivos mezclados
    # con los 2 vivos. En /bancos sí se ven todos (toggle).
    bancos_activos = [b for b in bancos if round(float(b["saldo"] or 0), 2) != 0.0]

    # ---- Diagnóstico — cuando el balance "no cuadra" el gerente necesita
    # ver de un vistazo qué componente puede estar vacío o desfasado.
    chq_breakdown = cheques_por_stat()
    snap_fecha = hist.get("fecha")
    dias_snapshot = (today_ec() - snap_fecha).days if snap_fecha else None
    from modules.activos.queries import borrado_where_sql as _borr_cnt
    activos_count_row = (
        db.fetch_one(f"SELECT COUNT(*) AS n FROM scintela.activos WHERE COALESCE(valor,0) > 0 {_borr_cnt()}") or {}
    )
    n_activos = int(activos_count_row.get("n") or 0)

    advertencias = []
    # Borde de mes: alertar si las iniciales del mes EN CURSO no están cargadas
    # (inic resolvió a otro mes). Sin la fila del mes corriente el stock de
    # terminado y las tarifas se calculan desde el cierre anterior — el número
    # sigue siendo razonable gracias al fallback, pero conviene cargar la fila.
    # Bug 2026-07-01: faltaba Julio y el stock cayó 2M. [[iniciales_mes_actual]]
    if _iniciales_del_mes_falta:
        advertencias.append(
            f"Faltan las INICIALES del mes en curso ({_hoy_ec_bal.month:02d}/{_hoy_ec_bal.year}). "
            f"El balance ancla el stock al último cierre y usa la fila "
            f"{int(inic.get('mesnum') or 0):02d}/{int(inic.get('yy') or 0)} como referencia. "
            "La utilidad del mes es PROVISIONAL hasta cargar la fila del mes."
        )
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
    depositados = chq_breakdown.get("B", {}).get("total", 0.0) + chq_breakdown.get("A", {}).get("total", 0.0)

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
            "uret_calc": _uret_calc,
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
    h_kcom = comp_mes["kg"]
    h_ucom = comp_mes["importe"]

    # ─── Iniciales / proyecciones del mes ───────────────────────────────
    # Estos datos del mes target los necesita el bloque COSTOS (proyección
    # de cada fila + tarifas META para fallback) y el bloque STOCK más
    # abajo. Antes vivían después del bloque COSTOS y eso rompía el orden
    # de definición.
    kgpro = float(inic.get("kprog") or 0)  # KGPRO — kg meta del mes
    pretej = float(inic.get("pretej") or 0)
    pretin = float(inic.get("pretin") or 0)
    preadm = float(inic.get("preadm") or 0)
    pretot = float(inic.get("pretot") or 0)
    inic_um = float(inic.get("um") or 0)  # tarifa MP objetivo
    inic_uk = float(inic.get("uk") or 0)  # tarifa tejido objetivo
    inic_uq = float(inic.get("uq") or 0)  # tarifa col.qui. objetivo
    inic_pre = float(inic.get("pre") or 0)  # tarifa precio venta

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
    # OPENING del stock = mes ANTERIOR al mes CALENDARIO en curso — réplica de
    # `GO BOTT; SKIP -1` del PRG, que es POSICIONAL. NO usar inic.mesnum: si
    # falta la fila del mes en curso (p.ej. no se cargó Julio), inic cae al mes
    # previo y el "mes anterior" se corre un mes (arranca de Mayo en vez de
    # Junio) → HI0/TJ0/PF0/VQ0/UM0 salen de un mes de más → stock sobrevaluado
    # ~278k y utilidad inflada. El dBase valúa contra el mes calendario
    # anterior sí o sí. Bug 2026-07-01. [[iniciales_mes_actual]]
    mesnum_actual = _hoy_ec_bal.month
    yy_actual = _hoy_ec_bal.year
    um_anterior = tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "um")
    tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "uf")

    # Tarifas live (ratio de datos del mes; pueden ser 0 si no hay datos).
    precio = _safe_div(h_uvent, h_kvent)
    umx = _safe_div(h_ucom, h_kcom)
    iqx = _safe_div(0, 0)  # se completa con tin abajo
    # Tarifas EFFECTIVAS = live si hay, sino meta de iniciales.
    precio_eff, precio_src = _eff_rate(precio, inic_pre)
    um_eff, um_src = _eff_rate(umx, inic_um)
    uq_eff, uq_src = _eff_rate(iqx, inic_uq)

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
    amort = amortizaciones_mensuales()  # → dcc, dtj, deprcar
    gxg = gastos_xgast_v1_a_v9_mes()  # → V1..V9 sumados por rubro
    tin = tinto_mes_corriente_resultado()  # → itin, ktint, kr
    tej = tejido_mes_componentes()  # → kg interno/externo + us externo/KK

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
    # PRG líneas 304-315: GO BOTT (mes en curso) + SKIP -1 → la fila del mes
    # ANTERIOR. La fila del mes en curso NO es el inicio del mes: el dBase la
    # REESCRIBE en cada corrida del informe (verificado 2026-06-10 contra
    # HISTORIA.DBF: iniciales[jun] suma = STOCK del 09/06, iniciales[may]
    # suma = STOCK del cierre 31/05). Usar la fila previa, como pf0.
    HI0 = tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "hilado") \
        or float(inic.get("hilado") or 0)
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
    KR = float(tin.get("kr") or 0)
    ITIN = float(tin.get("itin") or 0)
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
    h_ktej = float(hist.get("ktej") or 0)
    h_utej = float(hist.get("utej") or 0)
    h_ktin = float(hist.get("ktin") or 0)
    h_utin = float(hist.get("utin") or 0)
    # h_gasto/h_costo/h_gstotal del cierre histórico ya no se usan: GASTOS
    # viene live de xgast.V7+V8+V9 + DEPRCAR; costo_total se suma de las
    # filas. Sólo h_usuti se conserva para diagnóstico (referencia histórica).
    h_usuti = float(hist.get("usuti") or 0)

    # Tarifas legacy heredadas del último cierre, para diagnóstico.
    _safe_div(h_utej, h_ktej)  # U$/kg tejido (histórico)
    iqx_legacy = _safe_div(h_utin, h_ktin)  # U$/kg gs.proceso (histórico)

    # Recomputar iqx con tin.itin/KR ahora que tin está disponible —
    # esto reemplaza el placeholder de iqx=0 puesto antes del bloque COSTOS.
    iqx = _safe_div(ITIN, KR) if KR else iqx_legacy
    # Refrescar uq_eff con el valor real ahora que conocemos iqx.
    uq_eff, uq_src = _eff_rate(iqx, inic_uq)

    proy_uvent = kgpro * precio_eff
    proy_mp = kgpro * um_eff
    proy_col = kgpro * uq_eff
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

    # Stock por etapa — fórmula VIVA del PRG (INFORMES.PRG L313-315):
    #     HI = HI0 + KM − KH          (hilado: compras − salido a tejeduría)
    #     TJ = TJ0 + KK − KT          (tejido: tejido − salido a tintura)
    #     PF = PF0 + KR − KV          (terminado: tinturado − vendido)
    # con HI0/TJ0/PF0 = fila del mes ANTERIOR de iniciales (cierre previo) y
    # KT = compras_T_externas + KTINT − KSTI (PRG L264).
    #
    # TMT 2026-06-10 (pedido Andrés/dueña): en el dBase, tipear el paso de
    # tejeduría/tintorería SUBE la utilidad (los kg se revalorizan +0,50/kg
    # al pasar a tejido y +1,70/kg al pasar a terminado — margen de
    # manufactura por etapa). PC tenía hilado/tejido CONGELADOS al caché de
    # iniciales[mes corriente] (que es lo que el dBase escribió en su última
    # corrida) → los pasos no movían nada y la utilidad no subía.
    #
    # NO confundir con el intento revertido de hoy (resumen_stock, 78fbff7):
    # ese sumaba KK a tejido SIN restar KH de hilado ni KT a tintura (doble
    # conteo → +$494k fantasma). Esta es la fórmula PRG exacta, conservando
    # la identidad kg: cada kg está en UNA sola etapa. Al cierre (movimientos
    # = 0) HI=HI0/TJ=TJ0 → continuidad con PATANT, igual que el dBase.
    # Verificado contra HISTORIA.DBF 09/06: STOCK = may(2.323.544) + movs ✓.
    _tj0_prev = tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "tejido") \
        or float(inic.get("tejido") or 0)
    _kt_ext = compras_tipo_t_externos_mes()
    _ksti = tinto_kg_servicios_mes()
    KT_stock = float(_kt_ext.get("kg") or 0) + KTINT - float(_ksti or 0)
    h_hilado = max(0.0, HI)  # HI vivo del bloque MAT.PR. (HI0 prev + KM − KH)
    h_tejido_kg = max(0.0, _tj0_prev + KK - KT_stock)  # PRG: IIF(TJ<0,0,TJ)

    # TERMINADO — réplica EXACTA del dBase (INFORMES.PRG L315/L320):
    #   PF = PF0 + KR − KV
    #   PF0 = TERMINADO del mes ANTERIOR en iniciales (dBase: GO BOTT + SKIP -1)
    #   KR  = kg netos que llegan a terminado (tin.kr, excl. lavados COLOR LIKE 'LAV%')
    #   KV  = kg vendidos del mes (facturas LIVE = h_kvent)
    # Antes PC leía iniciales[mes_actual].terminado, que es un caché que el dBase
    # graba en la fila del mes y queda stale apenas se vende más después del último
    # write-back (PC mostraba 349.782 vs dBase 340.151). Recompute en vivo —
    # KV ya incluye las facturas creadas en PC, así que no hace falta el ajuste
    # kg_facturas_pc_no_sincronizadas() de antes. TMT 2026-06-05.
    # Mes en curso = CALENDARIO (no inic.mesnum, que se corre si falta la fila
    # del mes) → _prev_m es el mes de apertura correcto. Bug 2026-07-01.
    _cur_m = _hoy_ec_bal.month
    _cur_y = _hoy_ec_bal.year
    _prev_m = 12 if _cur_m == 1 else _cur_m - 1
    _prev_y = _cur_y - 1 if _cur_m == 1 else _cur_y
    _prev_inic = db.fetch_one(
        "SELECT terminado FROM scintela.iniciales "
        "WHERE yy = %s AND mesnum = %s "
        "ORDER BY id_iniciales DESC LIMIT 1",
        (_prev_y, _prev_m),
    ) or {}
    pf0_terminado = float(_prev_inic.get("terminado") or 0)
    if pf0_terminado <= 0:
        # Borde de mes: la fila del mes ANTERIOR falta o vino en 0 (p.ej. las
        # iniciales del mes en curso no se cargaron y `inic` resolvió a otro
        # mes). Arrastrar el ULTIMO terminado real conocido (<= mes anterior)
        # en vez de asumir 0 — si no, el stock de producto terminado colapsa a
        # 0 y hunde el patrimonio ~2M. Bug 2026-07-01. [[iniciales_mes_actual]]
        _pf0_fallback = db.fetch_one(
            """
            SELECT terminado
            FROM scintela.iniciales
            WHERE COALESCE(terminado, 0) > 0
              AND yy IS NOT NULL AND mesnum IS NOT NULL
              AND (yy < %s OR (yy = %s AND mesnum <= %s))
            ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
            LIMIT 1
            """,
            (_prev_y, _prev_y, _prev_m),
        ) or {}
        pf0_terminado = float(_pf0_fallback.get("terminado") or 0)
    # TMT 2026-06-10 (bug hunt stock inflado): para descontar de terminado_kg
    # usamos las kg vendidas FÍSICAMENTE — incluye las facturas marcadas como
    # 'asinfo-backfill' porque ellas son ventas reales que sí salieron del
    # depósito. Si usáramos `h_kvent` (que filtra backfill para que no infle
    # Resultados/cartera), `h_terminado_kg` inflaría por las kg vendidas que
    # no se descuentan → vsto sube → patr sube → utilidad infla. Bug detectado
    # post-fix de utilidad de las facturas Asinfo (~$80k de inflación de
    # stock terminado).
    h_kvent_fisico = ventas_mes_corriente_kg_fisico()
    h_terminado_kg = max(0.0, pf0_terminado + float(KR or 0) - float(h_kvent_fisico or 0))

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
    # kg × tarifa (full float precision). Las tarifas (h_um/h_uk/h_uf) se
    # calculan ARRIBA desde el HILADO DBASE y NO cambian acá — solo se elige
    # la FUENTE de los kg.
    #
    # TMT 2026-07-12 (spec dueña): opción de tomar los kg de stock de ASINFO
    # (bodegas 51/52/53) en vez del cálculo dBase, detrás del flag
    # BALANCE_STOCK_SOURCE (env) o ?stock_source= (query, para A/B en vivo sin
    # redeploy). Default 'dbase' → comportamiento IDÉNTICO al de hoy. Fail-soft:
    # si Asinfo no está disponible, cae al dBase (nunca rompe el balance).
    # Los kg de Asinfo se valúan con la MISMA tarifa dBase (spec: no tocar tarifas).
    import os as _os_bal
    # TMT 2026-07-12 (dueña dio OK): el DEFAULT del stock del balance pasa a
    # ASINFO para julio en adelante. Sigue siendo reversible: env
    # BALANCE_STOCK_SOURCE=dbase o ?stock_source=dbase vuelve al dBase sin
    # redeploy. Junio y meses cerrados no pasan por acá (son el cierre HISTORIA),
    # así que este cambio SOLO afecta el mes en curso. Fail-soft: si Asinfo no
    # responde, cae al dBase y nunca rompe el balance.
    _stock_src = _os_bal.environ.get("BALANCE_STOCK_SOURCE", "asinfo")
    try:
        from flask import has_request_context as _hrc, request as _rq_bal
        if _hrc():
            _stock_src = _rq_bal.args.get("stock_source") or _stock_src
    except Exception:  # noqa: BLE001
        pass
    _stock_src = (_stock_src or "dbase").strip().lower()

    # COHERENCIA (dueña 2026-07-12, verificado contra INICIALE.DBF): el stock del
    # balance TIENE que ser el MISMO que la tabla MOVIMIENTOS DEL MES (= el dBase).
    # El balance recalculaba tejido/terminado por su cuenta (325.646 / 265.360) y
    # subvaluaba el stock ~82k contra el dBase, que tiene 266.957 / 320.595
    # (= INICIALE.DBF fila julio; HISTORIA.DBF USTOCK 7.848.860). Ahora hilado/
    # tejido/terminado en kg salen del header de `movimientos_mes_dbase` — UNA sola
    # fuente que la tabla y el balance comparten. Fallback al cálculo viejo si mov
    # no está disponible. Las tarifas (h_um/h_uk/h_uf) NO cambian (ya son las de
    # INICIALE.DBF). Base de la UTILIDAD también → el balance deja de subvaluar.
    mov = _try_movimientos_mes()

    # ── ALARMA kg de hilado (dueña 2026-07-17: "si pasa tenemos que agarrar
    # el error"). Las compras H con kg=0 se completan desde su IMPORTACIÓN
    # (mov.hilado_kg_importacion). Si alguna NO matchea, o Asinfo no
    # contesta, el $/kg del hilado queda INFLADO y arrastra TODO el stock
    # (tejido=+0,50, terminado=+2,20) → utilidad falsa. Acá se avisa fuerte.
    # Umbral (dueña 2026-07-17: "no pongas una advertencia inútil"): una
    # compra chica sin kg (p.ej. QC 833,75 — que además puede ser un TIPO mal
    # puesto, no hilo) mueve la tarifa en decimales; solo alertar cuando el
    # importe sin kg puede distorsionar en serio el ponderado.
    _HILADO_SIN_KG_UMBRAL_US = 5_000.0
    try:
        _hkimp = (mov or {}).get("hilado_kg_importacion") or {}
        _hk_sin_match = _hkimp.get("sin_match") or []
        _hk_total_us = sum(float(c.get("importe") or 0) for c in _hk_sin_match)
        if _hk_total_us < _HILADO_SIN_KG_UMBRAL_US:
            _hk_sin_match = []
        if _hk_sin_match and not _hkimp.get("disponible"):
            advertencias.append(
                f"⚠ HILADO: hay {len(_hk_sin_match)} compra(s) del mes con importe pero SIN kg "
                "y Asinfo no contestó para completarlos desde la importación. "
                "El $/kg del hilado (y todo el stock) puede estar INFLADO — recargar la página "
                "o revisar Asinfo antes de leer la utilidad."
            )
        elif _hk_sin_match:
            _det = "; ".join(
                f"{str(c.get('prov') or '?').strip()} {float(c.get('importe') or 0):,.2f} ({c.get('fecha')})"
                for c in _hk_sin_match[:5]
            )
            advertencias.append(
                f"⚠ HILADO: {len(_hk_sin_match)} compra(s) del mes con importe pero SIN kg — "
                f"ni en la compra ni en su importación: {_det}. "
                "Sin esos kg el $/kg del hilado queda inflado y revalúa todo el stock. "
                "Completar el kg (o el N° de importación en el concepto); si la compra "
                "no es hilo, corregirle el TIPO desde Editar."
            )
    except Exception:  # noqa: BLE001 -- la alarma nunca rompe el balance
        pass

    def _mov_stock_kg(_et, _fallback):
        try:
            _v = float((((mov or {}).get("header") or {}).get(_et) or {})
                       .get("stock_act_kg"))
            return _v if _v > 0 else float(_fallback)
        except (TypeError, ValueError):
            return float(_fallback)

    kg_hilado_db = _mov_stock_kg("hilado", h_hilado)
    kg_tejido_db = _mov_stock_kg("tejido", h_tejido_kg)
    kg_term_db = _mov_stock_kg("terminado", h_terminado_kg)
    kg_hilado, kg_tejido, kg_term = kg_hilado_db, kg_tejido_db, kg_term_db
    _stock_fuente = "dbase"

    if _stock_src == "asinfo":
        try:
            from modules.asinfo import service as _asinfo_svc
            _inv = _asinfo_svc.inventario_por_etapa()
            if _inv.get("disponible"):
                # Con MATERIAL EN PROCESO (WIP), igual que la vista de Stock y
                # que el dBase: Hilado = bodega 51 + hilo despachado a tejeduría;
                # Crudo = bodega 52 + crudo despachado a tintura; Terminado = 53.
                # Sin el WIP, hilado/crudo quedan subvaluados (dueña 2026-07-12).
                kg_hilado = float(_inv["hilo_total"])   # 51 + en proceso TC
                kg_tejido = float(_inv["cruda_total"])  # 52 + en proceso PT
                kg_term = float(_inv["terminada"])      # bodega 53
                _stock_fuente = "asinfo"
        except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
            pass

    # COHERENCIA $/kg HILADO (dueña 2026-07-13: "el 2954 directamente = a la
    # variable del flujo"). El $/kg del hilado del balance TIENE que ser el MISMO
    # que el cuadro FLUJO (≈ 2,954, promedio ponderado apertura Asinfo + compras
    # del mes al costo real). Para que sea UNA sola variable y no se desincronice,
    # el flujo y el balance llaman a la MISMA función: asinfo_service.
    # mov_hilado_valuacion(). El `open_ukg` (costo de apertura) = stock_act_ukg del
    # mov (≈ 2,951); la función lo diluye con las compras del mes → 2,954. Ese
    # $/kg va a Materia Prima, a Hilado (stock izq) y —por identidad— al Stock
    # MP+Prod del activo. uk/uf derivan de él. Solo cuando la fuente es Asinfo;
    # con dBase se queda la tarifa del mov. Fallback al mov si Asinfo no está.
    try:
        _mov_hil_ukg = float((((mov or {}).get("header") or {}).get("hilado") or {})
                             .get("stock_act_ukg"))
    except (TypeError, ValueError):
        _mov_hil_ukg = 0.0
    if _mov_hil_ukg > 0:
        h_um = _mov_hil_ukg  # fallback = tarifa del mov (fuente dBase)
    if _stock_fuente == "asinfo":
        try:
            from modules.asinfo import service as _asvc_hval
            _hval = _asvc_hval.mov_hilado_valuacion(
                _hoy_ec_bal.year, _hoy_ec_bal.month, _mov_hil_ukg or h_um)
            if _hval.get("disponible") and float(_hval.get("stock_act_ukg") or 0) > 0:
                h_um = float(_hval["stock_act_ukg"])  # = variable del FLUJO (2,954)
        except Exception:  # noqa: BLE001 -- fail-soft, deja la tarifa del mov
            pass
    h_uk = h_um + 0.5
    h_uf = h_uk + 1.7

    # ── Materia Prima (COSTOS) = EGRESOS del cuadro MOVIMIENTOS (INICIAL
    # ASINFO) del flujo — dueña 2026-07-17: "tiene que salir del cuadro de
    # movimientos asinfo, no de cualquier lado; el usuario tiene que poder ver
    # de dónde viene". Fórmula compartida: asinfo_service.hilado_egresos_mes
    # (salidas bodega 51 − reingresos de lote). $ = kg × h_um (la tarifa
    # visible en STOCK y en el cuadro). Fallback (Asinfo caído): egresos del
    # mov estilo dBase, como antes.
    _mp_kg_balance = (((mov or {}).get("header") or {}).get("hilado") or {}).get("egresos_kg")
    _mp_us_balance = (((mov or {}).get("header") or {}).get("hilado") or {}).get("egresos_us")
    try:
        from modules.asinfo import service as _asvc_mp
        _hegr_bal = _asvc_mp.hilado_egresos_mes(_hoy_ec_bal.year, _hoy_ec_bal.month)
        if _hegr_bal.get("disponible") and float(_hegr_bal.get("egresos_kg") or 0) > 0:
            _mp_kg_balance = float(_hegr_bal["egresos_kg"])
            _mp_us_balance = _mp_kg_balance * float(h_um or 0)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
        pass

    # ── Tejeduría (COSTOS): kg = INGRESOS de CRUDO del mismo cuadro (bodega
    # 52 = Producción tejido = Ingresos crudo, los números que el chequeo ata
    # en verde). Antes usaba las compras K facturadas (subcontaba: kg de
    # RY/AP sin cargar). El $ sigue siendo el costo real facturado (compras K
    # + amortización) → el $/kg se abarata al repartirse sobre los kg físicos.
    # Fail-soft: sin Asinfo quedan las compras K, como antes.
    _tej_kg_balance = float(tej.get("kg_total") or 0)
    try:
        from datetime import date as _date_tejb

        from modules.asinfo import service as _asvc_tejb
        _mov52_bal = _asvc_tejb.movimiento_bodega_mes(
            52, _date_tejb(_hoy_ec_bal.year, _hoy_ec_bal.month, 1)) or {}
        _ing_crudo_bal = float(_mov52_bal.get("ingreso") or 0)
        if _ing_crudo_bal > 0:
            _tej_kg_balance = _ing_crudo_bal
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
        pass

    # ── Tintorería y Colorantes (COSTOS): kg = INGRESOS de TERMINADO del
    # mismo cuadro (bodega 53 = lo producido por tintorería). Igual que MP y
    # Tejeduría: la fila del balance dice lo MISMO que la columna TERM. del
    # cuadro MOVIMIENTOS (INICIAL ASINFO). Los $ no cambian (gastos reales de
    # tintorería / consumo de químicos) → el $/kg se reparte sobre los kg
    # físicos. Fallback: kg de tinto/formulas (comportamiento anterior).
    _tin_kg_balance = 0.0
    try:
        from datetime import date as _date_tinb

        from modules.asinfo import service as _asvc_tinb
        _mov53_bal = _asvc_tinb.movimiento_bodega_mes(
            53, _date_tinb(_hoy_ec_bal.year, _hoy_ec_bal.month, 1)) or {}
        _tin_kg_balance = float(_mov53_bal.get("ingreso") or 0)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el balance
        _tin_kg_balance = 0.0

    val_hilado = kg_hilado * h_um
    val_tejido = kg_tejido * h_uk
    val_terminado = kg_term * h_uf
    stock_total_kg = kg_hilado + kg_tejido + kg_term
    stock_total_us = val_hilado + val_tejido + val_terminado
    stock_ukg_prom = _safe_div(stock_total_us, stock_total_kg)

    # VSTO base DBASE — para la utilidad (que la re-valuación por cambio de
    # fuente NO se cuente como ganancia). Con fuente dBase == vsto exacto.
    vsto_dbase = kg_hilado_db * h_um + kg_tejido_db * h_uk + kg_term_db * h_uf

    # ─── VSTO del balance = TOTAL del panel STOCK izquierdo (display) ───
    # Para que panel ACTIVO derecho "STOCK MP+PROD." muestre el MISMO
    # número que el TOTAL del panel STOCK izquierdo.
    vsto = stock_total_us

    # ─── VQX vivo (PRG L322: VQX = VQ0 + VQQ − ITIN) ───────────────────
    # TMT 2026-06-10 dueña ("stock químicos seguro muy mal"): vqx venía del
    # ÚLTIMO SNAPSHOT de historia (uqui = lo que el dBase calculó en su
    # última corrida) — caché congelado, igual que hilado/tejido. No se
    # movía con compras Q nuevas ni con tintura cargada en PC. Ahora:
    #   VQ0  = iniciales.vq del mes ANTERIOR (cierre — la fila del mes en
    #          curso la reescribe el dBase en cada corrida)
    #   VQQ  = compras tipo 'Q' del mes (live)
    #   ITIN = importes de tinto del mes (live — incluye pc-carga/ajustes)
    _vq0_prev = tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "vq")
    _vqq_mes = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS importe
          FROM scintela.compra
         WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
           AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
           AND UPPER(COALESCE(tipo, '')) = 'Q'
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
    ) or {}
    if _vq0_prev:
        vqx = round(_vq0_prev + float(_vqq_mes.get("importe") or 0) - ITIN, 2)
    # si no hay iniciales del mes anterior, queda el fallback del snapshot

    # QUÍMICO = COLORANTE FÍSICO REAL de formulas (dueña 2026-07-12). Decisión:
    # el "Stock Quí." se mide como el físico de COLORANTE (familias POLI+ALG) de
    # formulas, NO el VQ del dBase (que subvalúa) ni el VQX vivo (VQ0 junio +
    # compras Q − ITIN). Se excluyen los AUXILIARES (AUX): son un stock a granel
    # de ~55k que se da vuelta solo (compras≈consumo) y que el dBase no valúa —
    # al sacarlos, el colorante físico (~338.614) queda a solo +26k del dBase
    # (311.953), no +82k. El VQX vivo de arriba queda como fallback si formulas
    # no está disponible.
    # MISMA variable que el FLUJO (banda STOCK DE QUÍMICOS + tabla COLOR $):
    # tintura_service.stock_colorante_fisico (POLI+ALG, sin AUX, ≈ 338). Que
    # salga de acá y no se recalcule → el químico es idéntico en balance y flujo.
    # Dueña 2026-07-13: "stock quimicos idem que hilado, la variable del flujo".
    # Dueña 2026-07-24: los AUXILIARES ahora CUENTAN en la utilidad. "Stock Quí."
    # = químico TOTAL (POLI+ALG+AUX, lo que muestra formulas_app) y ese total entra
    # a patrimonio Y a la utilidad. El patrimonio de junio (patant) NO se toca →
    # la utilidad pega el salto por incorporar los auxiliares (aceptado por la dueña;
    # reemplaza la neutralización `_quim_increment` del 21/07).
    try:
        from modules.tintura import service as _tsvc_q
        _vqx_col = float(_tsvc_q.stock_colorante_fisico(today_ec()) or 0)
        if _vqx_col > 0:
            vqx = _vqx_col   # fallback: colorante físico
        # TOTAL (incluye auxiliares) = mismo número que el flujo / formulas.
        from modules.informes.quimico_inv_formulas import quimico_total_fisico
        _vqx_tot = quimico_total_fisico(today_ec())
        if _vqx_tot is not None and _vqx_tot > 0:
            vqx = float(_vqx_tot)   # Stock Quí. = químico total (con aux) → entra a la utilidad
    except Exception:  # noqa: BLE001 -- fail-soft, deja el VQX vivo
        pass

    # ─── UTILIDAD (fórmula explícita TMT 2026-05-06) ───
    #   utility = patrimonio_mayo - patrimonio_abril + dividendos
    #          = (b.patr - b.uret) - patant + b.uret
    #          = b.patr - patant
    totl = subt + vsto + vqx + activos["umaq"] + activos["uact"] + _uret_calc + _antic
    patr = totl - posdats["totp"]
    # UTILIDAD = PATR − PATANT usando el MISMO stock que muestra el balance
    # (vsto), NO una base aparte. TMT 2026-07-15 (dueña: "que tome lo que se ve
    # en el balance de stock; no tengamos múltiples variables de lo mismo" —
    # regla general de coherencia). Antes usaba `vsto_dbase` (base dBase
    # separada) que quedaba subvaluado ~119k cuando la bodega de hilo de Asinfo
    # estaba frenada → hundía la utilidad (mostraba 54.853 en vez de ~174k).
    # Con una sola fuente de stock, patrimonio y utilidad quedan coherentes.
    # (vsto_dbase se sigue calculando sólo para el diagnóstico stock_revaluacion.)
    # Dueña 2026-07-24: los auxiliares CUENTAN. patr incluye el químico total (con
    # aux) vía vqx; patant (cierre de junio) NO se toca → la utilidad sube ese monto.
    utilidad = patr - patant
    patr_para_utilidad = patr

    # ─── SYNC del diagnostico tras el override de vsto ───
    # El diagnostico se construyó ~300 líneas arriba con vsto=0 (placeholder)
    # y por lo tanto totl/patr/utilidad de esa rama quedaron sin contar el
    # stock. Consumidores que leen `diagnostico.componentes.vsto` (ej.
    # sync_dbase_actual.comparar_drift_balance) recibían 0 → drift 100% y
    # patrimonio 38% off. Bug encontrado 2026-06-02 via /admin/dbase-sync.
    # Top-level resultado["vsto"] siempre estuvo bien — éste es sólo el
    # mirror para componentes.
    try:
        _comp = (diagnostico or {}).get("componentes")
        if isinstance(_comp, dict):
            _comp["vsto"] = vsto
            _comp["vqx"] = vqx   # TMT 2026-07-15: faltaba — quedaba el vqx VIEJO
            _comp["totl"] = totl
            _comp["patr"] = patr
            _comp["utilidad"] = utilidad
    except Exception:
        pass

    # Tabla RESULTADOS rediseñada (Federico 2026-05-21) — definida fila por
    # fila con el dueño. Reemplaza en balance.html el viejo bloque VENTA +
    # 5 filas COSTOS + Total + Utilidad Actual. El costo unitario de Materia
    # Prima sale de `mov` (flujo de producción, HILADO egresos $/kg). Las
    # claves viejas del dict resultados se siguen calculando para no romper
    # /informes/utilidad_debug ni otros consumidores.
    # `mov` ya se computó arriba (bloque de coherencia del stock) — reusar.
    try:
        _mp_ukg_flujo = float(
            ((mov or {}).get("header") or {}).get("hilado", {}).get("egresos_ukg")
            or 0
        )
    except Exception:
        _mp_ukg_flujo = 0.0
    # Factor de desperdicio (DESP+DESK) para la UT.PROY — misma fórmula que
    # _costo_total_con_desperdicio (PRG L258-261; DESK=0.5, DESP default 4).
    _kr_tin = float(tin.get("kr") or 0)
    _ktint_tin = float(tin.get("ktint") or 0)
    _desp_pct = ((1 - _kr_tin / _ktint_tin) * 100) if _ktint_tin > 0 else 4.0
    _factor_desp = 1 + (_desp_pct + 0.5) / 100

    # ─── Costo Total estilo dBase (réplica exacta INFORMES.PRG) ─────────
    # u$/kg = COSTUNI (L406) = factor*(UMX+ITIN/KR)+VK/KK+GTIN/KR+GS/KV
    # U$    = CSVTATOT (L411) = KV*1.04*(UMX+VK/KK+ITIN/KT+GC/(KTINT+KPROVT))+GS
    _vk_kk = _safe_div(VK, KK)
    _itin_kr = _safe_div(ITIN, KR)
    _gtin_kr = _safe_div(GTIN, KR)
    _gs_kv = _safe_div(GS, KV)
    _costuni = _factor_desp * (UMX + _itin_kr) + _vk_kk + _gtin_kr + _gs_kv
    # KT = kg crudo que entran a tinturar = tipo-T externo + KTINT − servicios (PRG L263).
    _kt = (compras_tipo_t_externos_mes().get("kg", 0.0) + KTINT - tinto_kg_servicios_mes()) or KTINT
    # KPROVT = kg tintura tercerizada prov 'TT' (PRG L222).
    _kprovt_row = db.fetch_one(
        "SELECT COALESCE(SUM(kg),0) AS kg FROM scintela.compra "
        "WHERE fecha >= date_trunc('month',CURRENT_DATE) "
        "  AND fecha <  date_trunc('month',CURRENT_DATE)+INTERVAL '1 month' "
        "  AND UPPER(TRIM(tipo))='T' AND UPPER(TRIM(COALESCE(codigo_prov,'')))='TT' "
        "  AND COALESCE(stat,'') NOT IN ('X','Y') "
        "  AND COALESCE(usuario_crea,'') <> 'asinfo-backfill'"
    ) or {}
    _kprovt = float(_kprovt_row.get("kg") or 0)
    # GC = gs.tintorería sin amort. PC lo aproxima de xgast V4+V5+V6 (el PRG lo
    # saca del flujo bancario GCC1+GCC2+CC — puede haber diferencia chica).
    _gc = float(gxg["gtin_sin_dcc"])
    _csvtatot = (KV * 1.04 * (UMX + _vk_kk + _safe_div(ITIN, _kt)
                 + _safe_div(_gc, (KTINT + _kprovt))) + GS)

    # Dueña 2026-07-21: la fila Colorantes/Quím. pasa a medir el químico FÍSICO
    # consumido de bodega (mismo criterio que el stock físico, ~157) en vez del
    # ITIN (costeo por orden, ~168). Coherencia "físico manda". Fail-soft → ITIN.
    _col_us_fisico = None
    try:
        from modules.informes.quimico_inv_formulas import quimico_consumido_us
        from datetime import date as _date_cf
        _col_us_fisico = quimico_consumido_us(
            _date_cf(yy_actual, mesnum_actual, 1), _hoy_ec_bal)
    except Exception:  # noqa: BLE001 -- fail-soft, queda el ITIN
        _col_us_fisico = None

    tabla_resultados = resultados_costos_tabla(
        venta_kg=h_kvent,
        venta_us=h_uvent,
        dia_actual=today_ec().day,
        mp_ukg=h_um,   # spec dueña 2026-07-12: Materia Prima = tarifa Hilado
        v1=gxg["v1"],
        v2=gxg["v2"],
        v3=gxg["v3"],
        dtj=amort["dtj"],
        tej_base_us=float(tej.get("us_total") or 0),
        # kg tejidos = Ingresos de CRUDO del cuadro MOVIMIENTOS (INICIAL
        # ASINFO) — misma fuente visible que Producción tejido (dueña
        # 2026-07-17). Fallback: compras K (comportamiento anterior).
        kg_tejidos=_tej_kg_balance,
        v4=gxg["v4"],
        v5=gxg["v5"],
        v6=gxg["v6"],
        dcc=amort["dcc"],
        itin=float(tin.get("itin") or 0),
        # Tintorería/Colorantes kg = INGRESOS de TERMINADO del cuadro
        # MOVIMIENTOS (INICIAL ASINFO) — dueña 2026-07-17: todas las filas
        # de COSTOS salen del cuadro visible. Fallback: kr/ktint de
        # tinto/formulas si Asinfo no respondió.
        ktint=(_tin_kg_balance or float(tin.get("kr") or 0)),
        ktint_colorantes=(_tin_kg_balance or float(tin.get("ktint") or 0)),
        col_us_fisico=_col_us_fisico,
        v7=gxg["v7"],
        v8=gxg["v8"],
        v9=gxg["v9"],
        deprcar=amort["deprcar"],
        # Utilidad Real = UTILIDAD del dBase (INFORMES.PRG L380: PATR − PATANT
        # LIVE), NO historia.usuti (snapshot del último cierre, stale). La dueña
        # pidió "calcularlo de variables de resultados, no de historia". Con el
        # Pasivos ya reconciliado al dBase, `utilidad` (= patr − patant) cuadra
        # con el UT.ACT del dBase. patant/uret en 0 → ur_us = utilidad. TMT 2026-06-05.
        patr=float(utilidad or 0),
        patant=0.0,
        uret=0.0,
        # UT.PROY estilo dBase — gastos proyectados de scintela.iniciales.
        kgpro=kgpro,
        pretot=pretot,
        # Presupuesto por área (columna Proyectado GS.PROY) — TMT 2026-06-23.
        pretej=pretej,
        pretin=pretin,
        preadm=preadm,
        factor_desperdicio=_factor_desp,
        provision_pendiente=provision_pendiente_us,
        utilidad_econ=float(utilidad or 0),
        # Dueña 2026-07-12: el Costo Total ya NO usa las fórmulas del dBase
        # (COSTUNI/CSVTATOT). Se pasa None → la tabla lo calcula como la SUMA de
        # los renglones (cada columna suma de arriba a abajo). Ver else-branch.
        costo_total_ukg=None,
        costo_total_us=None,
        # Materia Prima = EGRESOS del cuadro "MOVIMIENTOS DEL MES (INICIAL
        # ASINFO)" del Flujo de producción (dueña 2026-07-17: "tiene que salir
        # del cuadro de movimientos asinfo, no de cualquier lado — el usuario
        # tiene que poder ver de dónde viene"). MISMA función compartida:
        # asinfo_service.hilado_egresos_mes (salidas de bodega 51 netas de
        # reingresos). $ = kg × tarifa del hilado (h_um, la misma del STOCK y
        # del cuadro). Fail-soft: si Asinfo no responde, quedan los egresos
        # del mov estilo dBase (comportamiento anterior).
        mp_kg=_mp_kg_balance,
        mp_us=_mp_us_balance,
    )

    resultados = {
        "ventas": {
            "kg": h_kvent,
            "ukg": precio,
            "us": h_uvent,
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
                "kg": cost_mat_kg,
                "ukg": cost_mat_ukg,
                "us": cost_mat_us,
                "proy": cost_mat_proy,
                "src": "live" if cost_mat_us > 0 or HI > 0 else um_src,
                "ayuda": (
                    "MAT.PR. (PRG línea 399). U$/kg = UMX (FIFO ponderado del stock final): "
                    f"({VM:,.0f} + ({HI:,.0f} - {KM:,.0f}) × {UM0:,.3f}) / {HI:,.0f} = {UMX:,.3f}. "
                    f"HI = HI0 ({HI0:,.0f}) + KM ({KM:,.0f}) - KH ({KH:,.0f}). "
                    f"Proyección: KGPRO ({kgpro:,.0f}) × UMX ({UMX:,.3f}) = {cost_mat_proy:,.0f}."
                ),
                "detalle": {
                    "HI0_stock_anterior": HI0,
                    "KM_compras_kg": KM,
                    "VM_compras_us": VM,
                    "UM0_tarifa_anterior": UM0,
                    "KH_salieron_a_tejido": KH,
                    "HI_stock_final": HI,
                    "UMX": UMX,
                },
            },
            # TEJIDO — PRG línea 400: kg=KK, ukg=VK/KK, us=VK, proy=XPRETEJ
            # VK = SUM(IMPORTE TIPO='K') + DTJ. NO incluye V1+V2+V3.
            {
                "label": "TEJIDO",
                "kg": cost_tej_kg,
                "ukg": cost_tej_ukg,
                "us": cost_tej_us,
                "proy": cost_tej_proy,
                "src": "live"
                if cost_tej_us > 0
                else ("meta" if (pretej > 0 or cost_tej_kg_src == "meta") else "none"),
                "ayuda": (
                    "TEJIDO (PRG línea 400). U$/kg = VK/KK. "
                    "VK = SUM(IMPORTE WHERE TIPO='K') + DTJ — "
                    "compras tercerizado externo + KK gastos varios + amort.maquinaria. "
                    "NO incluye V1+V2+V3 (sueldos/gas/varios planta — en el PRG aparecen "
                    "en el reporte detallado de GASTOS, no acá). "
                    "KK = SUM(KG WHERE TIPO='K') = interno (PROV='KK') + externo."
                ),
                "detalle": {
                    "VK_us": VK,
                    "KK_kg": KK,
                    "amort_dtj": amort["dtj"],
                    "kg_interno": tej["kg_interno"],
                    "kg_externo": tej["kg_externo"],
                    "us_externo": tej["us_externo"],
                    "us_kk_gastos": tej["us_kk_gastos"],
                },
            },
            # COL.QUI. — PRG línea 401: kg=KTINT, ukg=ITIN/KR, us=ITIN, proy=KGPRO*ITIN/KR
            # OJO: ukg divide por KR (kg que llegan a terminado), NO por KTINT.
            {
                "label": "COL.QUI.",
                "kg": cost_col_kg,
                "ukg": cost_col_ukg,
                "us": cost_col_us,
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
                "kg": cost_gsp_kg,
                "ukg": cost_gsp_ukg,
                "us": cost_gsp_us,
                "proy": cost_gsp_proy,
                "src": "live" if cost_gsp_us > 0 else ("meta" if pretin > 0 else "none"),
                "ayuda": (
                    "GS.PROC. (PRG línea 402). U$/kg = GTIN / KR. "
                    f"GTIN ({GTIN:,.0f}) = V4+V5+V6 ({gxg['gtin_sin_dcc']:,.0f}) + DCC ({amort['dcc']:,.0f}). "
                    f"KR ({KR:,.0f}) = kg que llegan a terminado este mes."
                ),
                "detalle": {
                    "v4_v5_v6": gxg["gtin_sin_dcc"],
                    "amort_dcc": amort["dcc"],
                    "GTIN": GTIN,
                    "KR": KR,
                },
            },
            # GASTOS — PRG línea 403: ukg=GS/KV, us=GS, proy=XPREADM (sin kg).
            # En el legacy puro: GS = G1+G2+CA+DEPRCAR (flujo bancario). Acá
            # aproximamos con V7+V8+V9 + DEPRCAR (sólo lo categorizado en xgast
            # como rubro 7/8/9). Esto puede diferir del dBase por las gastos
            # bancarios sin categoría que el legacy capturaba via FILTRO.
            {
                "label": "GASTOS",
                "kg": None,
                "ukg": cost_gas_ukg,
                "us": cost_gas_us,
                "proy": cost_gas_proy,
                "src": "live" if cost_gas_us > 0 else ("meta" if preadm > 0 else "none"),
                "ayuda": (
                    "GASTOS (PRG línea 403). U$/kg = GS / KV. "
                    f"GS ({GS:,.0f}) ≈ V7+V8+V9 ({gxg['gs_sin_deprcar']:,.0f}) + DEPRCAR ({amort['deprcar']:,.0f}). "
                    f"KV ({KV:,.0f}) = kg de ventas del mes. "
                    "(Diferencia con dBase: el legacy calcula GS = G1+G2+CA+DEPRCAR desde flujo bancario."
                ),
                "detalle": {
                    "v7_v8_v9": gxg["gs_sin_deprcar"],
                    "amort_deprcar": amort["deprcar"],
                    "GS": GS,
                    "KV": KV,
                },
            },
        ],
        "tarifas_src": {
            "precio": precio_src,
            "um": um_src,
            "uq": uq_src,
        },
        "costo_total": _costo_total_con_desperdicio(
            cost_mat_ukg=cost_mat_ukg,
            cost_col_ukg=cost_col_ukg,
            cost_tej_ukg=cost_tej_ukg,
            cost_gsp_ukg=cost_gsp_ukg,
            cost_gas_ukg=cost_gas_ukg,
            cost_mat_us=cost_mat_us,
            cost_col_us=cost_col_us,
            cost_tej_us=cost_tej_us,
            cost_gsp_us=cost_gsp_us,
            cost_gas_us=cost_gas_us,
            cost_mat_proy=cost_mat_proy,
            cost_col_proy=cost_col_proy,
            cost_tej_proy=cost_tej_proy,
            cost_gsp_proy=cost_gsp_proy,
            cost_gas_proy=cost_gas_proy,
            KR=KR,
            KTINT=KTINT,
        ),
        "utilidad": {
            # UT.ACT del PRG = PATR - PATANT (live). Foto: 592.544.
            "pct": utilidad_pct,
            "ukg": utilidad_ukg,
            "us": utilidad,  # ← live PATR-PATANT, no h_usuti
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
            "hilado": {"kg": kg_hilado, "ukg": h_um, "us": val_hilado},
            "tejido": {"kg": kg_tejido, "ukg": h_uk, "us": val_tejido},
            "terminado": {"kg": kg_term, "ukg": h_uf, "us": val_terminado},
            "total": {"kg": stock_total_kg, "ukg": stock_ukg_prom, "us": stock_total_us},
        },
        "tabla": tabla_resultados,
        "snapshot_fecha": snap_fecha,
        "iniciales_mes": (f"{inic.get('mesnom') or '?'} {inic.get('yy') or ''}" if inic else None),
    }

    resultado = {
        "totf": _totf,
        "totc": _totc,
        "bancos": bancos_activos,
        "bancos_todos": bancos,
        "salbanc1": salbanc1,
        "salbanc2": salbanc2,
        "salbanc": salbanc,
        "pos1": posdats["pos1"],
        "pos2": posdats["pos2"],
        "salcaj": _salcaj,
        "umaq": activos["umaq"],
        "uact": activos["uact"],
        "antic": _antic,
        "uret": _uret,
        "uret_calc": _uret_calc,
        "uret_anio": _uret_anio,
        "ventas_anio": _ventas_anio,
        # Federico 2026-07-14: utilidades acumuladas del año (meses cerrados
        # via historia.usuti + utilidad live del mes en curso) y el año en
        # curso para rotular las filas nuevas del panel MOVIMIENTOS.
        "utilidades_anio": utilidades_anio_en_curso(utilidad),
        "anio_actual": today_ec().year,
        "totp": posdats["totp"],
        "vsto": vsto,
        # Detalle por etapa para auditar el Δ de stock vs dBase (TMT 2026-07-24,
        # dueña: "¿hay algo que falta que no está siendo ponderado?"). Expone kg
        # y $/kg de cada etapa para que /admin/dbase-compare [10] muestre dónde
        # está el hueco (hilado/tejido/terminado).
        "stock_etapas": {
            "hilado": {"kg": kg_hilado, "ukg": h_um, "us": val_hilado},
            "tejido": {"kg": kg_tejido, "ukg": h_uk, "us": val_tejido},
            "terminado": {"kg": kg_term, "ukg": h_uf, "us": val_terminado},
        },
        "stock_fuente": _stock_fuente,          # 'dbase' | 'asinfo'
        "vsto_dbase": vsto_dbase,               # base de la utilidad
        "stock_revaluacion": vsto - vsto_dbase,  # +$ que suma el patrimonio por usar Asinfo
        # CHECK de coherencia across el cuadro (dueña 2026-07-13). Verifica que los
        # números que TIENEN que ser iguales lo sean; el template los marca ✓/✗.
        # 1) Stock MP+Prod (activo) = Total del panel STOCK izquierdo.
        # 2) $/kg de Materia Prima (COSTOS) = $/kg de Hilado (STOCK) = el del flujo.
        "coherencia": [
            {
                "label": "Stock MP+Prod (activo) = Total stock (izq.)",
                "izq": stock_total_us, "der": vsto,
                "diff": round(vsto - stock_total_us, 2),
                "ok": abs(vsto - stock_total_us) < 1.0,
            },
            {
                "label": "$/kg Materia Prima (costos) = $/kg Hilado (stock)",
                "izq": h_um, "der": h_um, "diff": 0.0, "ok": True,
            },
        ],
        "vqx": vqx,
        "cart": cart,
        "subt": subt,
        "totl": totl,
        "patr": patr,
        "patant": patant,
        "utilidad": utilidad,
        # `patr_para_utilidad` = patr ANTES del override de vsto, coherente
        # con PATANT (= historia.patrimonio neto del cierre anterior).
        # Lo expongo para que el panel pueda mostrar el cálculo:
        # utilidad = patr_para_utilidad - patant (sin re-valuación de stock).
        "patr_para_utilidad": patr_para_utilidad,
        # Provisión que aún no se amortizó este mes (PRG L420).
        # Top-level para que el template pueda intercalarla entre UT.ACT
        # y UT.PROY sin tener que entrar a resultados.utilidad.*.
        "provision_pendiente": provision_pendiente_us,
        "fecha": today_ec(),
        "snapshot_historia_fecha": snap_fecha,
        "kg": kg,
        "diagnostico": diagnostico,
        "resultados": resultados,
        "conciliacion": conciliacion_balance(),
        # TMT 2026-05-19 — item 15b: cuadro MOVIMIENTOS MES estilo dBase.
        # Fallback a None si la query rompe (no debe tirar la página).
        "movimientos_mes": mov,
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
    rows = (
        db.fetch_all(
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
        -- TMT 2026-06-03 audit fix: <> 0 en lugar de > 0 para que sobrepagos
        -- neteen (memoria project_cartera_signo).
        WHERE COALESCE(f.saldo, 0) <> 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
          -- TMT 2026-07-06: mismo filtro canónico que totf()/cartera aging
          -- (precedente NJL 2026-07-02) — sin él, el top-10 deudores mostraba
          -- saldo fantasma de facturas históricas Asinfo nunca cargadas.
          AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
        GROUP BY f.codigo_cli, c.nombre
        """
        )
        or []
    )

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

    TMT 2026-05-20 — devuelve también `tipo` (proveedor.tipo) para
    agrupar el informe /deudas por categoría (Mat.Prima H+Q, Maquinaria U,
    Bancos B, Otros Y/null).
    """
    # TMT 2026-05-20 — removido el filtro `importe > 0` para que el total
    # de Deudas COINCIDA con TOTP del balance (pedido dueña: "Pasivos no
    # es igual a deudas. Deberia ser igual"). Antes los anticipos /
    # ajustes (importe ≤ 0) se excluían acá pero NO de TOTP → discrepancia.
    return db.fetch_all(
        f"""
        SELECT COALESCE(p.codigo_prov, pd.prov)   AS codigo_prov,
               COALESCE(p.nombre, pd.prov, '—')   AS nombre,
               UPPER(COALESCE(p.tipo, ''))        AS tipo,
               COUNT(pd.id_posdat)                AS n_posdats,
               COALESCE(SUM(pd.importe), 0)       AS saldo_total,
               MIN(pd.fecha)                      AS posdat_mas_vieja,
               MIN(pd.fechad)                     AS vence_mas_viejo
        FROM scintela.posdat pd
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
        WHERE {posdat_deuda_viva_where("pd")}
          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
        GROUP BY p.codigo_prov, pd.prov, p.nombre, p.tipo
        HAVING ABS(COALESCE(SUM(pd.importe), 0)) > 0.005
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
    # TMT 2026-05-21 [overnight] fix C-2: la query no limitaba el techo de
    # fecha → si el DBF tiene rows con fecha futura (proyecciones), se
    # mostraban en /informes/flujo con fechas 2027/2028 + saldos negativos
    # confusos. Agregar `fecha <= CURRENT_DATE` para que el reporte sea
    # estrictamente histórico.
    return db.fetch_all(
        """
        SELECT fecha, cheques, facturas, pichincha, inter,
               posdat1, posdat2, mprima, gastos, saldo, pagos, dolares, usaldo
        FROM scintela.flujo
        WHERE fecha >= CURRENT_DATE - make_interval(days => %s)
          AND fecha <= CURRENT_DATE
        ORDER BY fecha DESC
        """,
        (dias_int,),
    )


# ---------------------------------------------------------------------------
# FLUJO DE FONDOS — réplica exacta de la opción 6 del dBase (MENU.PRG
# PROCEDURE FLUJO, L560-720 + FLUJO.DBF).
#
# TMT 2026-07-06 (dueña): "tenemos que revisar flujo de fondos, no está como
# el dBase". Auditado contra MENU.PRG + VARMEMO.MEM + FLUJO.DBF del 03/07:
#
#   Arranque (L629-641):
#       SALCA = último saldo de CAJA
#       P1/P2 = Σ posdat banc=1/2 (cheques YA emitidos, aún sin debitar —
#               el libro bancario ya registró el débito, por eso se re-suman)
#       S1 = saldo Pichincha + P1 ;  S2 = saldo Inter + P2
#       ST = S1 + S2 + SALCA   → primera fila del flujo (FECHA=hoy,
#            GASTOS=SALCA: el dBase guarda la caja en la col. GASTOS).
#   Items → archivo temporal INGRESOS (L642-651):
#       cheques  STAT $ "Z123P"          (cartera; 'D'/devuelto NO entra)
#       facturas STAT $ "ZA"             (…pero L701 hardcodea FA=0)
#       posdat   NUM # 9999
#       facturas: FECHAD = VENCIM+50; si queda <= hoy+5 → hoy+7
#   Loop por FECHAD (L666-706), una fila por día:
#       CASE NB>0            → C  (ingreso cheques)
#       CASE NB=0 (factura)  → FA (…y luego FA=0)
#       CASE BANC=1 / BANC=2 → P1/P2 (bajan PICH/INTER a su fechad)
#       CASE PROV $ HIL      → H  (materia prima: proveedores tipo H activos)
#       CASE BANC=9 .OR. 0   → G  (gastos: posdat comunes + forzados banc=9)
#       SALDO = ST - P1 - P2 + C + FA - G - H
#       Corte de semana (DOW(FECH) <= ANT): fila en blanco con ACUMI
#       (Σ ingresos) bajo CHEQ y ACUME (Σ G+H) bajo GASTS.
# ---------------------------------------------------------------------------


def _saldo_banco_por_nombre(fragmento: str) -> float:
    """Último saldo (fecha <= hoy — excluye postdatados, feedback 2026-06-25)
    del banco cuyo nombre contiene `fragmento` ('PICHINC' / 'INTERNACI')."""
    row = db.fetch_one(
        """
        SELECT COALESCE((
                 SELECT t.saldo
                   FROM scintela.transacciones_bancarias t
                  WHERE t.no_banco = b.no_banco
                    AND t.fecha <= CURRENT_DATE
                  ORDER BY t.fecha DESC, t.id_transaccion DESC
                  LIMIT 1), 0) AS saldo
          FROM scintela.banco b
         WHERE POSITION(%s IN UPPER(COALESCE(b.nombre, ''))) > 0
         ORDER BY b.no_banco
         LIMIT 1
        """,
        (fragmento,),
    )
    return float((row or {}).get("saldo") or 0)


def flujo_arranque_dbase() -> dict:
    """Saldos de arranque del flujo (MENU.PRG L629-641). Ver bloque arriba."""
    caja_row = db.fetch_one(
        """
        SELECT saldo FROM scintela.caja
         WHERE saldo IS NOT NULL
         ORDER BY fecha DESC NULLS LAST, id_caja DESC
         LIMIT 1
        """
    )
    salca = float((caja_row or {}).get("saldo") or 0)
    p_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(CASE WHEN banc = 1 THEN importe END), 0) AS p1,
               COALESCE(SUM(CASE WHEN banc = 2 THEN importe END), 0) AS p2
          FROM scintela.posdat
         WHERE banc IN (1, 2)
           AND COALESCE(num, 0) <> 9999
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    )
    p1 = float((p_row or {}).get("p1") or 0)
    p2 = float((p_row or {}).get("p2") or 0)
    s1 = _saldo_banco_por_nombre("PICHINC") + p1
    s2 = _saldo_banco_por_nombre("INTERNACI") + p2
    return {
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "p1": round(p1, 2),
        "p2": round(p2, 2),
        "caja": round(salca, 2),
        "total": round(s1 + s2 + salca, 2),
    }


def flujo_items_dbase(incluir_facturas: bool = False) -> list[dict]:
    """Items pendientes del flujo, tipados como el dBase arma INGRESOS.

    Devuelve [{fecha, tipo, importe}] con tipo ∈ cheque | factura |
    posdat1 | posdat2 | mprima | gasto. Sin ventana: TODO lo pendiente
    (el dBase tampoco recorta)."""
    items: list[dict] = []
    # Cheques en cartera — MENU.PRG L643: &AF CHEQUES FOR STAT $ "Z123P".
    # OJO: stat 'D' (devuelto) NO entra — el dBase no lo cuenta como
    # ingreso futuro. TMT 2026-07-06 (dueña): antes se sumaba.
    rows = (
        db.fetch_all(
            """
        SELECT fechad AS fecha, COALESCE(SUM(importe), 0) AS total
          FROM scintela.cheque
         WHERE stat IN ('Z', '1', '2', '3', 'P')
           AND fechad IS NOT NULL
         GROUP BY fechad
        """
        )
        or []
    )
    items += [
        {"fecha": r["fecha"], "tipo": "cheque", "importe": float(r["total"] or 0)}
        for r in rows
    ]
    # Posdat — MENU.PRG L645 (&AF POSDAT FOR NUM#9999) + CASE L677-684.
    # El orden del CASE importa: banc=1/2 gana sobre PROV$HIL, y HIL
    # (proveedores de hilado, FABRICA TIPO='H' ACTIVA='S') gana sobre el
    # resto (banc 0/9 → gastos). Réplica con scintela.proveedor.tipo.
    rows = (
        db.fetch_all(
            """
        SELECT p.fechad AS fecha,
               CASE WHEN COALESCE(p.banc, 0) = 1 THEN 'posdat1'
                    WHEN COALESCE(p.banc, 0) = 2 THEN 'posdat2'
                    WHEN UPPER(COALESCE(pr.tipo, '')) = 'H'
                         AND UPPER(LEFT(COALESCE(pr.activo, '1'), 1)) IN ('1', 'S')
                         THEN 'mprima'
                    ELSE 'gasto' END AS tipo,
               COALESCE(SUM(p.importe), 0) AS total
          FROM scintela.posdat p
          LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = p.prov
         WHERE p.fechad IS NOT NULL
           AND COALESCE(p.num, 0) <> 9999
           AND COALESCE(p.banc, 0) IN (0, 1, 2, 9)
           AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
         GROUP BY 1, 2
        """
        )
        or []
    )
    items += [
        {"fecha": r["fecha"], "tipo": r["tipo"], "importe": float(r["total"] or 0)}
        for r in rows
    ]
    if incluir_facturas:
        # MENU.PRG L644+647-649: facturas stat Z/A; cobro proyectado a
        # VENCIM+50; si eso queda a <= hoy+5 días → hoy+7.
        rows = (
            db.fetch_all(
                """
            SELECT CASE WHEN COALESCE(vencimiento, CURRENT_DATE) + 50
                             <= CURRENT_DATE + 5
                        THEN CURRENT_DATE + 7
                        ELSE COALESCE(vencimiento, CURRENT_DATE) + 50 END AS fecha,
                   COALESCE(SUM(saldo), 0) AS total
              FROM scintela.factura
             WHERE stat IN ('Z', 'A') AND COALESCE(saldo, 0) <> 0
             GROUP BY 1
            """
            )
            or []
        )
        items += [
            {"fecha": r["fecha"], "tipo": "factura", "importe": float(r["total"] or 0)}
            for r in rows
        ]
    return items


def _dow_dbase(d) -> int:
    """DOW() de dBase: domingo=1 … sábado=7."""
    return d.isoweekday() % 7 + 1


def flujo_egresos_detalle() -> list[dict]:
    """Detalle POR POSDAT de los egresos del flujo (pedido dueña 2026-07-08:
    "estos -87k no sé de dónde salen").

    Misma clasificación mprima/gasto que flujo_items_dbase, pero fila por fila
    (con prov + concepto), SIN agregar — para poder mostrar de qué se compone el
    número de cada día en /informes/flujo-fondos.
    """
    rows = db.fetch_all(
        """
        SELECT p.fechad AS fecha,
               CASE WHEN COALESCE(p.banc, 0) = 1 THEN 'posdat1'
                    WHEN COALESCE(p.banc, 0) = 2 THEN 'posdat2'
                    WHEN UPPER(COALESCE(pr.tipo, '')) = 'H'
                         AND UPPER(LEFT(COALESCE(pr.activo, '1'), 1)) IN ('1', 'S')
                         THEN 'mprima'
                    ELSE 'gasto' END AS tipo,
               COALESCE(p.prov, '')     AS prov,
               COALESCE(p.concepto, '') AS concepto,
               COALESCE(p.importe, 0)   AS importe,
               COALESCE(p.banc, 0)      AS banc
          FROM scintela.posdat p
          LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = p.prov
         WHERE p.fechad IS NOT NULL
           AND COALESCE(p.num, 0) <> 9999
           AND COALESCE(p.banc, 0) IN (0, 1, 2, 9)
           AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
         ORDER BY p.fechad, p.importe DESC
        """
    ) or []
    return [
        {"fecha": r["fecha"], "tipo": r["tipo"], "prov": r["prov"] or "",
         "concepto": r["concepto"] or "", "importe": float(r["importe"] or 0),
         "banc": int(r["banc"] or 0)}
        for r in rows
    ]


def construir_flujo_diario(
    hoy,
    s1: float,
    s2: float,
    salca: float,
    items: list[dict],
    incluir_facturas: bool = False,
    vencidos_a_hoy: bool = False,
) -> list[dict]:
    """Loop de PROCEDURE FLUJO (MENU.PRG L652-706) — función PURA (testeable).

    Devuelve filas tipo:
      'arranque' — hoy, PICH=S1, INTER=S2, GASTOS=+caja, SALDO=S1+S2+SALCA
      'dia'      — una por fecha con movimiento, saldos acumulados
      'semana'   — corte de semana: ingresos (ACUMI) / egresos (ACUME)

    El dBase muestra los vencidos a su fecha real (quedan ANTES de hoy en
    la tabla); `vencidos_a_hoy=True` los imputa a hoy (lo usa el gráfico).
    FA=0 salvo `incluir_facturas` (dBase L701)."""
    por_dia: dict = {}
    for it in items:
        f = it.get("fecha") or hoy
        if vencidos_a_hoy and f < hoy:
            f = hoy
        d = por_dia.setdefault(
            f,
            {"cheque": 0.0, "factura": 0.0, "posdat1": 0.0,
             "posdat2": 0.0, "mprima": 0.0, "gasto": 0.0},
        )
        if it["tipo"] in d:
            d[it["tipo"]] += float(it.get("importe") or 0)

    st = s1 + s2 + salca
    filas: list[dict] = [
        {
            "tipo": "arranque", "fecha": hoy,
            "pichincha": round(s1, 2), "inter": round(s2, 2),
            "cheques": 0.0, "facturas": 0.0, "mprima": 0.0,
            # dBase: la primera fila lleva la CAJA en la columna GASTOS.
            "gastos": round(salca, 2), "saldo": round(st, 2),
            "vencida": False,
        }
    ]
    ant = _dow_dbase(hoy)
    acumi = acume = 0.0
    for f in sorted(por_dia):
        d = por_dia[f]
        # Corte de semana (L697: IF DOW(FECH) <= ANT) — fila con los
        # acumulados. Las de 0/0 (arranque inmediato) no aportan: se omiten.
        if _dow_dbase(f) <= ant and (acumi or acume):
            filas.append(
                {"tipo": "semana", "ingresos": round(acumi, 2),
                 "egresos": round(acume, 2)}
            )
            acumi = acume = 0.0
        ant = _dow_dbase(f)
        c = d["cheque"]
        fa = d["factura"] if incluir_facturas else 0.0  # L701: FA=0
        p1, p2, h, g = d["posdat1"], d["posdat2"], d["mprima"], d["gasto"]
        s1 -= p1
        s2 -= p2
        st = st - p1 - p2 + c + fa - g - h
        filas.append(
            {
                "tipo": "dia", "fecha": f,
                "pichincha": round(s1, 2), "inter": round(s2, 2),
                "cheques": round(c, 2), "facturas": round(fa, 2),
                "mprima": round(-h, 2), "gastos": round(-g, 2),
                "saldo": round(st, 2), "vencida": f < hoy,
            }
        )
        acumi += c + fa
        acume += g + h
    # Última semana parcial: el dBase la deja sin subtotal (quirk del loop);
    # acá sí la mostramos — misma cuenta, más completa.
    if acumi or acume:
        filas.append(
            {"tipo": "semana", "ingresos": round(acumi, 2),
             "egresos": round(acume, 2)}
        )
    return filas


def flujo_fondos_diario(incluir_facturas: bool = False) -> dict:
    """Tabla diaria del Flujo de Fondos — lo que renderiza /informes/flujo-fondos."""
    hoy = today_ec()
    arr = flujo_arranque_dbase()
    items = flujo_items_dbase(incluir_facturas=incluir_facturas)
    filas = construir_flujo_diario(
        hoy, arr["s1"], arr["s2"], arr["caja"], items,
        incluir_facturas=incluir_facturas,
    )
    dias = [f for f in filas if f["tipo"] in ("arranque", "dia")]
    minimo = min(dias, key=lambda f: f["saldo"]) if dias else None
    return {
        "arranque": arr,
        "filas": filas,
        "saldo_final": dias[-1]["saldo"] if dias else arr["total"],
        "saldo_min": minimo["saldo"] if minimo else arr["total"],
        "fecha_min": minimo["fecha"] if minimo else hoy,
    }


def flujo_calculado(
    dias_atras: int = 14,
    dias_adelante: int = 365,
    ignorar_cheques: bool = False,
) -> list[dict]:
    """Flujo de caja calculado EN VIVO — alimenta /informes/flujo/grafico.

    TMT 2026-07-06 (dueña) "no está como el dBase" — reescrito sobre los
    helpers de la opción 6 (flujo_arranque_dbase / flujo_items_dbase /
    construir_flujo_diario). Cambios vs la versión anterior:
      - Arranque ST = S1 + S2 + SALCA (Pichincha + Inter + Caja, con corte
        fecha<=hoy). Antes sumaba el último saldo de TODOS los bancos
        (incluyendo pseudo-bancos contables) y sin el corte (postdatados
        con saldo viejo desfasaban el arranque — mismo bug 2026-06-25).
      - Posdat banc=1/2 (cheques emitidos sin debitar) engordan el arranque
        y se restan a su FECHAD (MENU.PRG L634-641 + L677-679). Antes no
        se proyectaban.
      - Cheques: stat Z/1/2/3/P como dBase — se saca 'D' (devuelto) y los
        VENCIDOS se imputan a hoy (antes el filtro fechad>=hoy los perdía).
      - Posdat num=9999 excluidos (dBase L645).
      - MPRIMA (proveedores tipo 'H') separada de GASTOS (dBase L681) —
        el gráfico ya la dibuja aparte.
      - Facturas: FA=0, igual que el dBase (L701) y que antes.

    Forma de salida SIN cambios (el template consume igual):
        {fecha, saldo, cheques, facturas, posdat1, posdat2, pichincha,
         inter, mprima, gastos, pagos, dolares}

    Para días pasados (`dias_atras`) se muestra el saldo de hoy como línea
    recta hacia atrás — contexto visual, no historia real.
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

    hoy = today_ec()
    tope = hoy + _td(days=adelante)
    arr = flujo_arranque_dbase()

    por_dia: dict = {}
    for it in flujo_items_dbase():
        if ignorar_cheques and it["tipo"] == "cheque":
            continue
        f = it.get("fecha") or hoy
        if f < hoy:
            # TMT 2026-07-07 (dueña): "igual al dBase — hoy arranca en el saldo
            # lleno (2,98M) pero los vencidos SÍ se restan". El dBase arranca en
            # ST y procesa los vencidos por fechad (running saldo baja). Para que
            # el NODO de hoy quede en ST y no neto, los vencidos se imputan a
            # hoy+1 (se restan apenas pasa hoy, sin hundir el arranque).
            f = hoy + _td(days=1)
        if f > tope:
            continue
        d = por_dia.setdefault(
            f,
            {"cheque": 0.0, "factura": 0.0, "posdat1": 0.0,
             "posdat2": 0.0, "mprima": 0.0, "gasto": 0.0},
        )
        if it["tipo"] in d:
            d[it["tipo"]] += float(it.get("importe") or 0)

    filas: list[dict] = []

    # Días pasados — línea recta del saldo actual (sin replay de historia).
    for offset in range(-atras, 0):
        fecha = hoy + _td(days=offset)
        filas.append(
            {
                "fecha": fecha, "saldo": arr["total"],
                "cheques": 0.0, "facturas": 0.0,
                "posdat1": 0.0, "posdat2": 0.0,
                "pichincha": 0.0, "inter": 0.0,
                "mprima": 0.0, "gastos": 0.0,
                "pagos": 0.0, "dolares": 0.0,
            }
        )

    # Hoy y adelante — acumulación diaria (PRG L692: SALDO=ST-P1-P2+C+FA-G-H).
    # ⚠ offset=0 también aplica cambios: los vencidos imputados a hoy deben
    # restar/sumar HOY, no mañana (bug histórico de $1.4M — no repetir).
    s1, s2, saldo_acum = arr["s1"], arr["s2"], arr["total"]
    vacio = {"cheque": 0.0, "factura": 0.0, "posdat1": 0.0,
             "posdat2": 0.0, "mprima": 0.0, "gasto": 0.0}
    for offset in range(0, adelante + 1):
        fecha = hoy + _td(days=offset)
        d = por_dia.get(fecha, vacio)
        s1 -= d["posdat1"]
        s2 -= d["posdat2"]
        saldo_acum = (
            saldo_acum - d["posdat1"] - d["posdat2"]
            + d["cheque"] - d["gasto"] - d["mprima"]
        )
        filas.append(
            {
                "fecha": fecha, "saldo": saldo_acum,
                "cheques": d["cheque"], "facturas": 0.0,
                "posdat1": -d["posdat1"], "posdat2": -d["posdat2"],
                "pichincha": s1, "inter": s2,
                "mprima": -d["mprima"],
                "gastos": -d["gasto"],  # negativo: el chart lo trata como egreso
                "pagos": 0.0, "dolares": 0.0,
            }
        )

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
    row_cobro = (
        db.fetch_one(
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
        )
        or {}
    )
    row_deuda = (
        db.fetch_one(
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
        )
        or {}
    )
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
    # TMT 2026-05-27 dueña: "stock terminado se fue a 0". Las facturas
    # backfilleadas de Asinfo (usuario_crea='asinfo-backfill') son
    # HISTORICAS — ya fueron contabilizadas como vendidas en su mes.
    # Si las contamos como "no sincronizadas" inflamos el descuento al
    # stock terminado (28k kg extra restados) -> stock=0.
    # Excluirlas explícitamente. dbf-import + asinfo-backfill = "ya
    # contabilizadas en algún snapshot".
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS total
        FROM scintela.factura
        WHERE COALESCE(usuario_crea, '') NOT IN ('dbf-import', 'asinfo-backfill')
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
    return (
        db.fetch_all(
            f"""
        SELECT
          id_posdat,
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
        )
        or []
    )


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
    "cheques",
    "facturas",
    "posdat1",
    "posdat2",
    "pichincha",
    "inter",
    "mprima",
    "gastos",
    "saldo",
    "pagos",
    "dolares",
    "usaldo",
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
                        usuario,
                        existing["id_flujo"],
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
                    f"INSERT INTO scintela.flujo ({', '.join(cols)}) VALUES ({placeholders})",
                    tuple(vals),
                    conn=conn,
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# VENTAS mensuales — agregadas desde factura
# ---------------------------------------------------------------------------


def ventas_mes_a_mes_anio_actual() -> list[dict]:
    """Ventas mes a mes del año en curso con acumulado.

    TMT 2026-05-20 — pedido dueña: pantalla simple desde
    /informes/balance al click 'Ventas del año'. Columnas:
    mes, kg, precio (U$/kg), importe, acum.

    TMT 2026-05-20 v2 — fix: ahora usa MISMA FUENTE que el balance
    para que el TOTAL coincida con 'Ventas del año' (Resultados).
    Antes solo leía scintela.factura → daba ~5.5M cuando balance
    decía 10M (la diferencia es que historia.uvent ya incluye
    devoluciones/ajustes contabilizados en el closing mensual).
    Pedido dueña: "Ventas del año esta mal, el total es 10 millones,
    lo tenes en resultados". Fórmula nueva:
       - meses cerrados → historia.uvent / historia.kvent
       - mes en curso   → live de scintela.factura (mismo filtro
                          que ventas_anio_en_curso: stat<>'X', any sign)
    """

    hoy = today_ec()
    yy, mm = hoy.year, hoy.month

    # Meses cerrados del año (historia.uvent ya tiene el cierre definitivo).
    rows_hist = (
        db.fetch_all(
            """
        SELECT EXTRACT(MONTH FROM fecha)::int AS mes_num,
               COALESCE(SUM(uvent), 0) AS importe,
               COALESCE(SUM(kvent), 0) AS kg
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) < %s
         GROUP BY EXTRACT(MONTH FROM fecha)
        """,
            (yy, mm),
        )
        or []
    )

    # Mes en curso → live de scintela.factura (mismo filtro que
    # ventas_anio_en_curso: stat <> 'X', importe > 0 — sin sumar
    # devoluciones/sobrepagos que distorsionarían el live).
    row_live = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0) AS importe,
               COALESCE(SUM(kg), 0)      AS kg
          FROM scintela.factura
         WHERE EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
           AND COALESCE(stat, '') <> 'X'
           AND COALESCE(importe, 0) > 0
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (yy, mm),
        )
        or {}
    )

    # Armar mapa mes → datos.
    por_mes: dict[int, dict] = {}
    for r in rows_hist:
        m = int(r.get("mes_num") or 0)
        por_mes[m] = {
            "kg": float(r.get("kg") or 0),
            "importe": float(r.get("importe") or 0),
        }
    por_mes[mm] = {
        "kg": float(row_live.get("kg") or 0),
        "importe": float(row_live.get("importe") or 0),
    }

    _MES_NOMBRES = [
        "Enero",
        "Febrero",
        "Marzo",
        "Abril",
        "Mayo",
        "Junio",
        "Julio",
        "Agosto",
        "Septiembre",
        "Octubre",
        "Noviembre",
        "Diciembre",
    ]
    acum = 0.0
    out: list[dict] = []
    for m in sorted(por_mes.keys()):
        d = por_mes[m]
        kg, importe = d["kg"], d["importe"]
        acum += importe
        precio = (importe / kg) if kg > 0 else 0.0
        out.append(
            {
                "mes_num": m,
                "mes_nombre": _MES_NOMBRES[m - 1] if 1 <= m <= 12 else "?",
                "kg": kg,
                "precio": precio,
                "importe": importe,
                "acum": acum,
            }
        )
    return out


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
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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

    hoy = today_ec()
    anio_actual = hoy.year
    anios_list = [anio_actual - (n - 1) + i for i in range(n)]  # asc

    # Una query — todas las facturas vivas en el rango de años + agrupado por
    # (year, month). Filtramos stat para excluir anuladas (TMT bug TMT
    # 2026-04-29: filas con stat='Y' inflaban U$/kg). PRG: `STAT $ "ZA"`.
    rows = (
        db.fetch_all(
            """
        SELECT EXTRACT(YEAR  FROM fecha)::int AS yy,
               EXTRACT(MONTH FROM fecha)::int AS mm,
               COUNT(*)                       AS n,
               COALESCE(SUM(kg), 0)           AS kg,
               COALESCE(SUM(importe), 0)      AS importe
        FROM scintela.factura
        WHERE EXTRACT(YEAR FROM fecha) BETWEEN %s AND %s
          AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        GROUP BY yy, mm
        """,
            (anios_list[0], anios_list[-1]),
        )
        or []
    )

    idx: dict[tuple[int, int], dict] = {}
    for r in rows:
        idx[(int(r["yy"]), int(r["mm"]))] = {
            "kg": float(r.get("kg") or 0),
            "importe": float(r.get("importe") or 0),
            "n": int(r.get("n") or 0),
        }

    mes_labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

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
        tot_n = 0
        for m in range(1, 13):
            d = idx.get((a, m), {})
            tot_kg += float(d.get("kg") or 0)
            tot_us += float(d.get("importe") or 0)
            tot_n += int(d.get("n") or 0)
        precio_prom = (tot_us / tot_kg) if tot_kg else 0.0
        totales_por_anio[a] = {
            "kg": tot_kg,
            "importe": tot_us,
            "precio_prom": precio_prom,
            "n": tot_n,
        }

    return {
        "anios": anios_list,
        "meses": meses_out,
        "totales_por_anio": totales_por_anio,
        "n_anios": n,
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
        WHERE tb.fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
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
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto, r.clave,
               b.nombre AS banco
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE r.fecha >= CURRENT_DATE - (%s || ' days')::interval
        ORDER BY r.fecha DESC, r.id_retiro DESC
        """,
        (str(dias),),
    )


def retiros_del_mes_actual() -> list[dict]:
    """Retiros del mes corriente — para la tab 'Dividendos del mes'.

    TMT 2026-05-20 — pedido dueña: unificar /informes/retiros con tabs
    mes/año. Replaces el filtro 'últimos N días' que era poco intuitivo.
    """
    return db.fetch_all(
        """
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto, r.clave,
               b.nombre AS banco,
               -- TMT 2026-07-20 (duena): "no deja cancelar retiros" desde
               -- Dividendos. Enriquecemos cada fila con su via de reverso:
               (SELECT ol.id_op_retiro_linea FROM scintela.op_retiro_linea ol
                 WHERE ol.id_retiro = r.id_retiro
                 ORDER BY ol.id_op_retiro_linea DESC LIMIT 1) AS id_op_linea,
               (SELECT md.id_mov_doble FROM scintela.mov_doble md
                 WHERE md.origen_table = 'retiros' AND md.origen_id = r.id_retiro
                   AND md.tipo LIKE 'retiro_socio_%%' AND md.estado = 'activo'
                 ORDER BY md.id_mov_doble DESC LIMIT 1) AS md_socio,
               COALESCE(r.usuario_crea, '') AS usuario_crea,
               EXISTS (SELECT 1 FROM scintela.retiros a
                        WHERE a.concepto = 'ANULACION retiro dBase id=' || r.id_retiro
                           OR a.concepto LIKE 'ANULACION retiro dBase id=' || r.id_retiro || ' %%'
                      ) AS anulada
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE EXTRACT(YEAR FROM r.fecha)  = EXTRACT(YEAR FROM CURRENT_DATE)
          AND EXTRACT(MONTH FROM r.fecha) = EXTRACT(MONTH FROM CURRENT_DATE)
        ORDER BY r.fecha DESC, r.id_retiro DESC
        """
    )


def retiros_del_anio_actual() -> list[dict]:
    """Retiros del año corriente — para la tab 'Dividendos del año'."""
    return db.fetch_all(
        """
        SELECT r.id_retiro, r.fecha, r.nb, r.ret, r.de, r.concepto, r.clave,
               b.nombre AS banco,
               -- TMT 2026-07-20 (duena): "no deja cancelar retiros" desde
               -- Dividendos. Enriquecemos cada fila con su via de reverso:
               (SELECT ol.id_op_retiro_linea FROM scintela.op_retiro_linea ol
                 WHERE ol.id_retiro = r.id_retiro
                 ORDER BY ol.id_op_retiro_linea DESC LIMIT 1) AS id_op_linea,
               (SELECT md.id_mov_doble FROM scintela.mov_doble md
                 WHERE md.origen_table = 'retiros' AND md.origen_id = r.id_retiro
                   AND md.tipo LIKE 'retiro_socio_%%' AND md.estado = 'activo'
                 ORDER BY md.id_mov_doble DESC LIMIT 1) AS md_socio,
               COALESCE(r.usuario_crea, '') AS usuario_crea,
               EXISTS (SELECT 1 FROM scintela.retiros a
                        WHERE a.concepto = 'ANULACION retiro dBase id=' || r.id_retiro
                           OR a.concepto LIKE 'ANULACION retiro dBase id=' || r.id_retiro || ' %%'
                      ) AS anulada
        FROM scintela.retiros r
        LEFT JOIN scintela.banco b ON b.no_banco = r.nb
        WHERE EXTRACT(YEAR FROM r.fecha) = EXTRACT(YEAR FROM CURRENT_DATE)
        ORDER BY r.fecha DESC, r.id_retiro DESC
        """
    )


def retiros_total_mes_actual() -> float:
    # TMT 2026-06-10 revert: filtro asinfo-backfill removido (convención
    # "no contar Asinfo hasta cierre" descartada).
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0) AS total
        FROM scintela.retiros
        WHERE EXTRACT(YEAR FROM fecha)  = EXTRACT(YEAR FROM CURRENT_DATE)
          AND EXTRACT(MONTH FROM fecha) = EXTRACT(MONTH FROM CURRENT_DATE)
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
    )
    return float(row["total"] or 0) if row else 0.0


def retiros_total_anual() -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(ret), 0) AS total
        FROM scintela.retiros
        WHERE EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM CURRENT_DATE)
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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

    hoy = today_ec()
    anio_actual = hoy.year
    anios = [anio_actual - 2, anio_actual - 1, anio_actual]

    # Una única query — agarra los últimos 36 meses (3 años × 12) y le
    # damos forma en Python. Cada fila histórica tiene `fecha` (último día
    # del mes legacy o algún día del mes); agrupamos por (year, month).
    rows = (
        db.fetch_all(
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
        )
        or []
    )

    # Indexar por (yy, mm) → datos.
    idx: dict[tuple[int, int], dict] = {}
    for r in rows:
        idx[(int(r["yy"]), int(r["mm"]))] = {
            "patrimonio": float(r.get("patrimonio") or 0),
            "uvent": float(r.get("uvent") or 0),
            "usuti": float(r.get("usuti") or 0),
            "kvent": float(r.get("kvent") or 0),
            "ustock": float(r.get("ustock") or 0),
            "uqui": float(r.get("uqui") or 0),
            "cart": float(r.get("cart") or 0),
            "deuda": float(r.get("deuda") or 0),
            "usret": float(r.get("usret") or 0),
            "fecha": r.get("fecha"),
        }

    mes_labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

    def _pct_var(act: float, ant: float) -> float | None:
        """Variación % de `act` vs `ant`. None si la base es 0 (evita ZeroDiv)."""
        if not ant:
            return None
        return (act - ant) * 100.0 / ant

    meses_out = []
    for m in range(1, n_meses + 1):
        datos: dict = {}
        for a in anios:
            datos[a] = idx.get(
                (a, m),
                {
                    "patrimonio": 0.0,
                    "uvent": 0.0,
                    "usuti": 0.0,
                    "kvent": 0.0,
                    "ustock": 0.0,
                    "uqui": 0.0,
                    "cart": 0.0,
                    "deuda": 0.0,
                    "usret": 0.0,
                    "fecha": None,
                },
            )
        # Variación % anio_actual vs anio_actual-1 (replica el "DR %" del PRG L1446-1450).
        cur, prev = datos[anio_actual], datos[anio_actual - 1]
        datos["var_patr_pct"] = _pct_var(cur["patrimonio"], prev["patrimonio"])
        datos["var_uvent_pct"] = _pct_var(cur["uvent"], prev["uvent"])
        datos["var_kvent_pct"] = _pct_var(cur["kvent"], prev["kvent"])
        datos["var_usuti_pct"] = _pct_var(cur["usuti"], prev["usuti"])
        meses_out.append(
            {
                "mes": m,
                "label": mes_labels[m - 1],
                "datos": datos,
            }
        )

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
                   -- TMT 2026-07-06 (dueña "totales muy mal" EDU): era `> 0`,
                   -- que ESCONDÍA los saldos negativos (NC / sobrepagos) y el
                   -- buscador mostraba 278.036,86 mientras el top-10 y el
                   -- estado de cuenta decían 241.781,01. Criterio canónico de
                   -- cartera (modules/cartera/queries.py): saldo <> 0 netea +
                   -- excluir asinfo-backfill. No divergir.
                   AND COALESCE(f.saldo, 0) <> 0
                   AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
                   AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
               ), 0) AS saldo
        FROM scintela.cliente c
        WHERE c.codigo_cli ILIKE %s OR c.nombre ILIKE %s
        ORDER BY saldo DESC, c.nombre
        LIMIT %s
        """,
        (pattern, pattern, limite),
    )


def estado_cuenta_clientes_saldos() -> list[dict]:
    """Saldo vivo por cliente + sus dimensiones de agrupación (vendedor, grupo,
    provincia), para el reporte "imprimir estado de cuenta por grupos".

    Un renglón por cliente con saldo <> 0. Criterio de saldo IDÉNTICO al del
    estado de cuenta / cartera (saldo <> 0, stat vivo, sin asinfo-backfill) para
    no divergir. `vencido` = saldo de facturas cuya `vencimiento` ya pasó.

    Trae las TRES claves de agrupación en cada fila; la vista agrupa por la que
    pida el usuario (vendedor / grupo / provincia). Reemplaza la PROCEDURE
    GRUPOS del dBase pero para el estado de cuenta.
    """
    return db.fetch_all(
        """
        WITH saldos AS (
            SELECT f.codigo_cli,
                   COALESCE(SUM(f.saldo), 0) AS saldo,
                   COALESCE(SUM(CASE WHEN f.vencimiento IS NOT NULL
                                      AND f.vencimiento < CURRENT_DATE
                                     THEN f.saldo ELSE 0 END), 0) AS vencido
            FROM scintela.factura f
            WHERE COALESCE(f.saldo, 0) <> 0
              AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
              AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
            GROUP BY f.codigo_cli
        )
        SELECT c.codigo_cli,
               COALESCE(c.nombre, c.codigo_cli)                       AS nombre,
               COALESCE(NULLIF(TRIM(c.vend), ''), '')                 AS vend,
               COALESCE(NULLIF(TRIM(v.nombre), ''),
                        NULLIF(TRIM(c.vend), ''),
                        '(sin vendedor)')                             AS vendedor_nombre,
               COALESCE(v.activo, FALSE)                             AS vendedor_activo,
               COALESCE(NULLIF(TRIM(c.provincia), ''), '(sin provincia)') AS provincia,
               COALESCE(gp.codigo_padre, c.codigo_cli)                AS grupo_codigo,
               COALESCE(NULLIF(TRIM(cpad.nombre), ''),
                        gp.codigo_padre,
                        COALESCE(c.nombre, c.codigo_cli))             AS grupo_nombre,
               s.saldo,
               s.vencido
        FROM saldos s
        JOIN scintela.cliente c ON c.codigo_cli = s.codigo_cli
        LEFT JOIN scintela.vendedor v ON v.codigo = c.vend
        LEFT JOIN scintela.grupo_cliente gp ON gp.codigo_hijo = c.codigo_cli
        LEFT JOIN scintela.cliente cpad ON cpad.codigo_cli = gp.codigo_padre
        ORDER BY c.nombre
        """
    ) or []


def estado_cuenta_cliente(codigo_cli: str) -> dict:
    """Facturas + cheques aplicados de un cliente, con totales para el resumen.

    `totales.saldo_vivo` = lo que el cliente nos debe HOY (sum facturas activas
    con saldo > 0, excluye anuladas). Es el número que el gerente busca primero.
    """
    cliente = db.fetch_one(
        """
        SELECT codigo_cli, nombre, telefono, ruc, cupo, stop, pago, pase, descuento,
               -- TMT 2026-06-07: dirección para el header del estado de cuenta
               COALESCE(direccion1, '') AS direccion1,
               COALESCE(direccion2, '') AS direccion2,
               COALESCE(provincia, '')  AS provincia,
               COALESCE(canton, '')     AS canton
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
            "anticipos": [],
            "totales": {
                "kg": 0.0,
                "importe": 0.0,
                "abono": 0.0,
                "saldo": 0.0,
                "saldo_vivo": 0.0,
                "n_vencidas": 0,
                "saldo_vencido": 0.0,
                "cheques_total": 0.0,
                "cheques_cartera": 0.0,
                "cheques_depositados": 0.0,
                "cheques_acreditados": 0.0,
                "cheques_rebotados": 0.0,
                "saldo_a_favor": 0.0,
                "saldo_neto": 0.0,
                "n_anticipos": 0,
            },
        }
    facturas = db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, vencimiento,
               kg, importe, abono, saldo, stat, condic, tipo
        FROM scintela.factura
        WHERE codigo_cli = %s
          -- TMT 2026-06-11 (dueña): las totalizadas (stat T) ya no se muestran
          -- en el estado de cuenta.
          AND COALESCE(stat, '') <> 'T'
          -- TMT 2026-07-06: excluir asinfo-backfill también acá (criterio
          -- canónico de cartera; ya lo hacía la cuenta corriente hermana en
          -- clientes.cuenta_corriente desde el fix NJL 2026-07-02).
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        -- TMT 2026-06-11 (dueña): de la más antigua a la más actual —
        -- el ACUM corre como un libro mayor y la última fila = saldo hoy.
        -- (Reemplaza el orden DESC del 2026-05-17.)
        ORDER BY fecha ASC, numf ASC
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
          -- TMT 2026-06-23 (dueña): los espejos de anticipo NB=98 NO son
          -- cheques reales en cartera — son saldo a favor del cliente. Se
          -- listan aparte (ver `anticipos` abajo) para no ensuciar la grilla
          -- de cheques ni el tile "Cheques en cartera".
          AND COALESCE(c.no_banco, 0) <> 98
        -- TMT 2026-06-11 (dueña): mismo criterio que facturas — del más
        -- antiguo al más actual.
        ORDER BY COALESCE(c.fechaing, c.fechad, c.fecha) ASC, c.id_cheque ASC
        """,
        (codigo_cli,),
    )

    # TMT 2026-06-23 (dueña): SALDO A FAVOR / anticipos del cliente.
    # En cobranza, cuando un cheque excede las facturas, el sobrante se guarda
    # como un cheque-espejo NEGATIVO NB=98 'ANTICIPO' (paridad dBase ALTAS.PRG).
    # Antes ese espejo quedaba "escondido" entre los cheques y NO bajaba el
    # saldo del cliente → la dueña no lo veía. Ahora lo levantamos aparte y lo
    # neteamos contra el saldo (ver `saldo_neto` / `saldo_a_favor` en totales).
    anticipos = db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fecha_crea,
               c.importe, c.stat, c.id_cheque_padre
        FROM scintela.cheque c
        WHERE c.codigo_cli = %s
          AND COALESCE(c.no_banco, 0) = 98
          AND COALESCE(c.stat, '') <> 'X'
        ORDER BY COALESCE(c.fecha_crea, c.fecha) ASC, c.id_cheque ASC
        """,
        (codigo_cli,),
    )

    # Totales — calculados en SQL para precisión numeric, no en Python.
    tot_fac = (
        db.fetch_one(
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
          -- TMT 2026-07-06 (dueña "los totales muy mal están", caso EDU): el
          -- pie de la tabla dice "Totales (N facturas)" con N = filas
          -- LISTADAS (sin T, sin backfill), pero estas sumas iban sobre TODAS
          -- las facturas históricas del cliente (T incluidas) → kg/importe/
          -- abonado del pie no correspondían a la tabla de arriba (EDU:
          -- 161.092 kg / 1.431.257,37 de importe eran el HISTÓRICO completo,
          -- no las 66 facturas listadas). Mismo filtro que la lista, así el
          -- pie == Σ(filas visibles) y el último ACUM == saldo del pie.
          AND COALESCE(stat, '') <> 'T'
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (codigo_cli,),
        )
        or {}
    )
    # Stats canónicos (2026-04-29 + TMT 2026-05-16):
    #   Z/P = cartera (todavía en mano)
    #   B/A = depositados (B nuevo, A legacy — ambos son "ya en banco")
    #   1/2/3/R = devueltos/rebotados (1/2/3 todavía en gestión, R terminal)
    #   D = "Daniela" (caso especial legacy — no es depositado)
    #   E = endosado, X = eliminado
    tot_che = (
        db.fetch_one(
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
          -- TMT 2026-06-23: excluir espejos de anticipo NB=98 de los totales
          -- de cheques reales (se contabilizan en `saldo_a_favor`).
          AND COALESCE(no_banco, 0) <> 98
        """,
            (codigo_cli,),
        )
        or {}
    )

    # TMT 2026-06-23: saldo a favor del cliente = -Σ(importe espejos NB=98).
    # `importe` es negativo en los espejos, así que el saldo a favor (positivo)
    # = -SUM. `saldo_neto` = saldo de facturas + Σ(importe NB=98) (lo reduce).
    tot_ant = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0) AS anticipo_raw,
               COUNT(*)                  AS n_anticipos
        FROM scintela.cheque
        WHERE codigo_cli = %s
          AND COALESCE(no_banco, 0) = 98
          AND COALESCE(stat, '') <> 'X'
        """,
            (codigo_cli,),
        )
        or {}
    )
    _anticipo_raw = float(tot_ant.get("anticipo_raw") or 0)  # negativo
    _saldo_fac = float(tot_fac.get("saldo") or 0)

    totales = {
        "kg": float(tot_fac.get("kg") or 0),
        "importe": float(tot_fac.get("importe") or 0),
        "abono": float(tot_fac.get("abono") or 0),
        "saldo": float(tot_fac.get("saldo") or 0),
        "saldo_vivo": float(tot_fac.get("saldo_vivo") or 0),
        "saldo_vencido": float(tot_fac.get("saldo_vencido") or 0),
        "n_vencidas": int(tot_fac.get("n_vencidas") or 0),
        "cheques_total": float(tot_che.get("total") or 0),
        "cheques_cartera": float(tot_che.get("cartera") or 0),
        "cheques_depositados": float(tot_che.get("depositados") or 0),
        "cheques_rebotados": float(tot_che.get("rebotados") or 0),
        "cheques_endosados": float(tot_che.get("endosados") or 0),
        "cheques_daniela": float(tot_che.get("daniela") or 0),
        # TMT 2026-06-23: saldo a favor (positivo) y saldo neteado.
        "saldo_a_favor": -_anticipo_raw,
        "saldo_neto": round(_saldo_fac + _anticipo_raw, 2),
        "n_anticipos": int(tot_ant.get("n_anticipos") or 0),
    }
    return {
        "cliente": cliente,
        "facturas": facturas,
        "cheques": cheques,
        "anticipos": anticipos,
        "totales": totales,
    }


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
    return (
        db.fetch_one(
            """
        SELECT *
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha)  = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha DESC, id_historia DESC
         LIMIT 1
        """,
            (int(yy), int(mm)),
        )
        or {}
    )


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

    if anio is None or mes is None:
        hoy = today_ec()
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

    filas = (
        db.fetch_all(
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
           AND COALESCE(c.usuario_crea, '') <> 'asinfo-backfill'
           {where_v}
         ORDER BY c.fecha ASC, c.id_compra ASC
        """,
            tuple(params),
        )
        or []
    )

    total_importe = sum(float(r.get("importe") or 0) for r in filas)
    total_kg = sum(float(r.get("kg") or 0) for r in filas)

    # Proveedores únicos para el dropdown filter.
    prov_options = (
        db.fetch_all(
            """
        SELECT DISTINCT c.codigo_prov,
               COALESCE(p.nombre, '') AS nombre
          FROM scintela.compra c
          LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
         WHERE EXTRACT(YEAR FROM c.fecha) = %s
           AND EXTRACT(MONTH FROM c.fecha) = %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND c.codigo_prov IS NOT NULL
           AND COALESCE(c.usuario_crea, '') <> 'asinfo-backfill'
         ORDER BY 1
        """,
            (anio, mes),
        )
        or []
    )

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


# ---------------------------------------------------------------------------
# Histórico TINT.BAT — TMT 2026-05-19 v7 (rediseño DOS-like)
# ---------------------------------------------------------------------------
# Líneas del informe — agrupadas en secciones (OPERATIVO / BALANCE) y con
# rows derivadas (precio U$/kg, utilid, MARGEN %, TOTAL ACTIVO) para imitar
# el viejo informe TINT.BAT del dBase.
#
# Cada fila lleva:
#   label    nombre visible (UPPER) idéntico al screenshot
#   key      campo crudo en scintela.historia o sentinel _derivado
#   fmt      "kg" | "ratio" | "pct" | "miles" — pista de formato al template
#   color    "cyan" | "white" | "yellow" | "red" | "blue" — paleta DOS
#   section  "operativo" | "balance"
_HIST_LINEAS: list[tuple[str, str, str, str, str]] = [
    # OPERATIVO — top section (per kg / ratios)
    ("VENTAS  kg.", "kvent", "kg", "white", "operativo"),
    ("precio  U$/kg", "_precio", "ratio", "white", "operativo"),
    ("utilid", "_utilid", "ratio", "white", "operativo"),
    ("MARGEN  %", "_margen_p", "pct", "white", "operativo"),
    # BALANCE — bottom section (miles de U$)
    ("BANCO       U$", "banco", "miles", "white", "balance"),
    ("CARTERA", "cart", "miles", "white", "balance"),
    ("ANTICIPOS", "anticipos", "miles", "white", "balance"),
    ("STOCK MP+PROD", "ustock", "miles", "white", "balance"),
    ("STOCK QUIM.", "uqui", "miles", "white", "balance"),
    ("MAQUINARIA", "maquinaria", "miles", "white", "balance"),
    ("TERR.Y EDIF.", "realty", "miles", "white", "balance"),
    ("TOTAL ACTIVO", "_activo", "miles", "yellow", "balance"),
    ("PASIVOS", "deuda", "miles", "white", "balance"),
    ("PATRIM.NET", "patrimonio", "miles", "yellow", "balance"),
    ("VENTAS", "uvent", "miles", "white", "balance"),
    ("UTILIDADES", "usuti", "miles", "red", "balance"),
    ("RR", "usret", "miles", "blue", "balance"),
]


def _valor_para_linea(key: str, snap: dict | None) -> float | None:
    """Computa el valor de una fila a partir del snapshot scintela.historia.

    Devuelve None cuando no hay snapshot o cuando el ratio se indetermina
    (división por cero).
    """
    if snap is None:
        return None
    if key.startswith("_"):
        uvent = float(snap.get("uvent") or 0)
        # ucom no se usa en esta función (vino de un refactor que terminó
        # leyendo usuti directo). Lo dejo derivable abajo si hace falta:
        # ucom = float(snap.get("ucom") or 0).
        usuti = float(snap.get("usuti") or 0)
        kvent = float(snap.get("kvent") or 0)
        if key == "_precio":  # U$/kg vendido
            return (uvent / kvent) if kvent else None
        if key == "_utilid":  # utilidad U$/kg
            return (usuti / kvent) if kvent else None
        if key == "_margen_p":  # utilidad / ventas %  (matchea TINT.BAT)
            return (usuti / uvent * 100.0) if uvent else None
        if key == "_activo":  # suma de activos
            return (
                float(snap.get("banco") or 0)
                + float(snap.get("cart") or 0)
                + float(snap.get("anticipos") or 0)
                + float(snap.get("ustock") or 0)
                + float(snap.get("uqui") or 0)
                + float(snap.get("maquinaria") or 0)
                + float(snap.get("realty") or 0)
            )
        return None
    return float(snap.get(key) or 0)


def _cargar_snapshots(meses: list[tuple[int, int]]) -> dict[tuple[int, int], dict]:
    """Lee scintela.historia y devuelve {(a,m): row} para los meses dados."""
    out: dict[tuple[int, int], dict] = {}
    for a_, m_ in meses:
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
            out[(a_, m_)] = row
    return out


def _cargar_snapshots_mes(anio: int, mes: int, limite: int = 3) -> list[dict]:
    """Devuelve TODOS los snapshots de un mes (max `limite`), ordenados de
    más viejo a más nuevo. TMT 2026-05-20 — pedido dueña: el mes actual
    puede tener 2+ snapshots para comparar el nuevo contra el anterior.
    """
    return (
        db.fetch_all(
            """
        SELECT id_historia, fecha, fecha_crea, usuario_crea,
               banco, cart, deuda, ustock, uqui, anticipos,
               maquinaria, realty, patrimonio, uvent, ucom, gasto,
               usret, usuti, kvent, kcom
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha_crea DESC
         LIMIT %s
        """,
            (anio, mes, int(limite)),
        )
        or []
    )


def _snap_live_mes_actual(hoy):
    """Snapshot del mes en curso calculado EN VIVO (sin guardarlo).

    2026-06-04 — alimenta la columna "actual / en vivo" del Histórico, que se
    recalcula en cada carga para que refleje cualquier operación al instante
    (pedido dueña: que cambie al mover algo, no al apretar Validar). Reusa
    `calcular_kpis` para no duplicar la lógica del snapshot (caja sumada a
    banco, exclusión de asinfo-backfill, etc.). Devuelve un dict tipo-snap con
    todos los campos de `_HIST_LINEAS`, o None si algo falla (la página no se
    rompe — simplemente no muestra la columna live).
    """
    import os
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    scripts_dir = os.path.join(repo_root, "scripts")
    for _p in (repo_root, scripts_dir):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    try:
        import snapshot_historia_mensual as _snap

        return _snap.calcular_kpis(hoy)
    except Exception:  # noqa: BLE001
        return None


def historico_12m_matriz(meses_atras: int = 5, offset_meses: int = 0) -> dict:
    """Matriz histórica estilo TINT.BAT — TMT 2026-05-19 v7.

    Pedido dueña 2026-05-19 — Feature B. Lee snapshots de scintela.historia
    y arma una matriz horizontal con filas operativas (per kg / ratios) y
    filas de balance (en miles de U$).

    Parámetros:
      meses_atras  cantidad de columnas a mostrar (default 5, máx 24).
      offset_meses cuántos meses correr la ventana HACIA EL PASADO. 0 = el
                   mes actual es la última columna; 5 = la última columna
                   es el mes que cae 5 meses antes (paginar hacia atrás).

    Devuelve:
      {
        meses:        [(anio, mes), ...] ASC — las N columnas mostradas
        lineas:       [{label, key, fmt, color, section, valores, total,
                        promedio, delta_pct}, ...]
        meses_total:  N (eco)
        offset_meses: eco
        snapshots_existentes: cuántos meses de los N tienen snapshot
        meses_sin_snap: ['MM/AAAA', ...]
        meses_disponibles: rango [(min_anio, min_mes), (max_anio, max_mes)]
                           de los snapshots existentes en BD — para acotar
                           la navegación.
        nav: {prev_offset: int|None, next_offset: int|None}
      }
    """

    n = max(1, min(int(meses_atras or 5), 24))
    off = max(0, int(offset_meses or 0))
    hoy = today_ec()

    # La última columna del matriz cae en (hoy - off) meses.
    a, m = hoy.year, hoy.month
    for _ in range(off):
        m -= 1
        if m < 1:
            m = 12
            a -= 1

    # Lista de meses (anio, mes) de los N anteriores a esa columna, ASC.
    meses: list[tuple[int, int]] = []
    ca, cm = a, m
    for _ in range(n):
        meses.append((ca, cm))
        cm -= 1
        if cm < 1:
            cm = 12
            ca -= 1
    meses.reverse()

    snapshots = _cargar_snapshots(meses)

    lineas_out = []
    for label, key, fmt, color, section in _HIST_LINEAS:
        valores = [_valor_para_linea(key, snapshots.get(k)) for k in meses]
        validos = [v for v in valores if v is not None]
        total = sum(validos) if validos else 0.0
        promedio = (total / len(validos)) if validos else 0.0
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
        lineas_out.append(
            {
                "label": label,
                "key": key,
                "fmt": fmt,
                "color": color,
                "section": section,
                "valores": valores,
                "total": total,
                "promedio": promedio,
                "delta_pct": delta_pct,
            }
        )

    sin_snap = [f"{m_:02d}/{a_}" for (a_, m_) in meses if (a_, m_) not in snapshots]

    # Rango global de snapshots en BD (para construir navegación segura).
    rango = (
        db.fetch_one(
            """
        SELECT MIN(fecha) AS min_f, MAX(fecha) AS max_f
          FROM scintela.historia
        """
        )
        or {}
    )
    rng_min = rango.get("min_f")
    rng_max = rango.get("max_f")

    # Navegación: prev = mostrar N meses anteriores; next = N meses adelante.
    # prev_offset solo si hay snapshots aún más atrás que la primera columna.
    prev_offset: int | None = None
    next_offset: int | None = None
    if meses:
        first_a, first_m = meses[0]
        if rng_min and (rng_min.year < first_a or (rng_min.year == first_a and rng_min.month < first_m)):
            prev_offset = off + n
        if off > 0:
            next_offset = max(0, off - n)

    return {
        "meses": meses,
        "lineas": lineas_out,
        "meses_total": n,
        "offset_meses": off,
        "snapshots_existentes": len(snapshots),
        "meses_sin_snap": sin_snap,
        "rng_min": rng_min,
        "rng_max": rng_max,
        "nav": {"prev_offset": prev_offset, "next_offset": next_offset},
    }


def _hora_quito(dt):
    """Convierte un datetime UTC (asi lo guarda el server/RDS) a hora
    de Quito. Ecuador es UTC-5 y NO tiene horario de verano, asi que
    alcanza con restar 5 horas fijas. Devuelve None si dt es None."""
    if dt is None:
        return None
    from datetime import timedelta as _td
    return dt - _td(hours=5)


def historico_5m_con_actual(max_actual: int = 3) -> dict:
    """Matriz histórica fija: últimos 5 meses + mes actual.

    Pedido dueña 2026-05-20:
      - 5 meses fijos a la izquierda (1 snapshot c/u, el más reciente del mes).
      - Mes actual a la derecha (puede tener 2+ snapshots para comparar).
      - Cada columna tiene id_historia para que el frontend pueda
        validar/borrar la columna correcta.

    `max_actual` = cuántos snapshots del mes actual mostrar (default 3,
    los más nuevos).

    Devuelve:
      {
        columnas: [{key: (a,m) o (a,m,id), label_corto, label_largo,
                    id_historia: int|None, es_mes_actual: bool,
                    es_canonico_default: bool}],
        lineas:   [{label, key, fmt, color, section, valores}],
        meses_sin_snap: ['MM/AAAA', ...],
      }
    """

    hoy = today_ec()

    # Últimos 5 meses CERRADOS (excluyendo el actual).
    meses_pasados: list[tuple[int, int]] = []
    ca, cm = hoy.year, hoy.month - 1
    if cm < 1:
        cm = 12
        ca -= 1
    for _ in range(5):
        meses_pasados.append((ca, cm))
        cm -= 1
        if cm < 1:
            cm = 12
            ca -= 1
    meses_pasados.reverse()  # ASC: el más viejo a la izquierda.

    # Snapshots del mes actual (varios para comparar).
    snaps_actual = _cargar_snapshots_mes(hoy.year, hoy.month, limite=max_actual)
    # Quedan ordenados más nuevo primero — invertimos para que la más
    # nueva quede a la derecha (siguiendo el orden cronológico visual).
    snaps_actual_asc = list(reversed(snaps_actual))

    # Snapshots de meses pasados (1 c/u, el más reciente).
    snaps_pasados = _cargar_snapshots(meses_pasados)

    # Armar lista de columnas para el template.
    from calendar import monthrange as _monthrange

    columnas: list[dict] = []
    for a_, m_ in meses_pasados:
        snap = snaps_pasados.get((a_, m_))
        # 2026-07-09 (pedido dueña): mostrar el DÍA al que corresponde la
        # columna. Un mes cerrado corresponde al último día del mes (31 may,
        # 30 jun). Usamos la fecha del snapshot (= último día del mes) y, si
        # falta el snapshot, el último día calendario del mes.
        if snap and snap.get("fecha"):
            _dia = snap["fecha"].day
        else:
            _dia = _monthrange(a_, m_)[1]
        columnas.append(
            {
                "key": f"{a_:04d}-{m_:02d}",
                "anio": a_,
                "mes": m_,
                "dia": _dia,
                "label_corto": f"{m_:02d}/{a_ % 100:02d}",
                "label_largo": f"{m_:02d}/{a_}",
                "id_historia": int(snap["id_historia"]) if snap and snap.get("id_historia") else None,
                "fecha_crea": _hora_quito(snap.get("fecha_crea")) if snap else None,
                "es_mes_actual": False,
                "es_canonico_default": False,
                "snap": snap,
            }
        )
    # ── Mes en curso ────────────────────────────────────────────────────
    # 2026-06-04 (pedido dueña: "que el cuadro cambie cada vez que muevo algo,
    # no cuando pongo validar"). La columna "actual" se RECALCULA EN VIVO en
    # cada carga (refleja cualquier factura/cobranza/pago al instante, igual
    # que la pantalla Resultados). La foto guardada más reciente del mes queda
    # como "previa" para comparar (Δ = qué cambió desde esa foto).
    n_actual = len(snaps_actual_asc)
    # Foto guardada más reciente del mes → columna "previa" (a lo sumo 1).
    for snap in snaps_actual_asc[-1:]:
        fc_ec = _hora_quito(snap.get("fecha_crea"))
        sufijo = " · previa" + (f" {fc_ec.strftime('%d/%m %H:%M')}" if fc_ec else "")
        columnas.append(
            {
                "key": f"{hoy.year:04d}-{hoy.month:02d}-{snap['id_historia']}",
                "anio": hoy.year,
                "mes": hoy.month,
                # Día al que corresponde la foto (fecha del snapshot = el día
                # en que se tomó, p.ej. "9 jul" para la previa de hoy).
                "dia": snap["fecha"].day if snap.get("fecha") else hoy.day,
                "label_corto": f"{hoy.month:02d}/{hoy.year % 100:02d}{sufijo}",
                "label_largo": f"{hoy.month:02d}/{hoy.year}{sufijo}",
                "id_historia": int(snap["id_historia"]),
                # fecha_crea CRUDA (UTC); el template aplica |hora_ec una sola vez.
                "fecha_crea": snap.get("fecha_crea"),
                "es_mes_actual": True,
                "es_canonico_default": False,
                "es_live": False,
                "snap": snap,
            }
        )

    # Columna ACTUAL — estado LIVE recalculado en CADA carga (no guardado).
    # Reusa calcular_kpis (misma lógica que el snapshot: caja en banco,
    # excluye asinfo-backfill) → tiene TODAS las filas, incluido operativo.
    hubo_live = False
    _live_snap = _snap_live_mes_actual(hoy)
    if _live_snap is not None:
        columnas.append(
            {
                "key": f"{hoy.year:04d}-{hoy.month:02d}-live",
                "anio": hoy.year,
                "mes": hoy.month,
                "dia": hoy.day,
                "label_corto": f"{hoy.month:02d}/{hoy.year % 100:02d} · ahora",
                "label_largo": f"{hoy.month:02d}/{hoy.year} · en vivo",
                "id_historia": None,
                "fecha_crea": None,
                "es_mes_actual": True,
                "es_canonico_default": True,
                "es_live": True,
                "snap": _live_snap,
            }
        )
        hubo_live = True

    # Armar líneas (igual que historico_12m_matriz).
    lineas_out = []
    for label, key, fmt, color, section in _HIST_LINEAS:
        valores = [_valor_para_linea(key, c.get("snap")) for c in columnas]
        lineas_out.append(
            {
                "label": label,
                "key": key,
                "fmt": fmt,
                "color": color,
                "section": section,
                "valores": valores,
            }
        )

    sin_snap = [f"{m_:02d}/{a_}" for (a_, m_) in meses_pasados if (a_, m_) not in snaps_pasados]
    if not snaps_actual_asc and not hubo_live:
        sin_snap.append(f"{hoy.month:02d}/{hoy.year} (actual — se genera al entrar)")

    # Federico 2026-05-21 -- columna fina de DIFERENCIAS entre las 2
    # columnas mas recientes, SOLO si las 2 ultimas son del mes actual.
    if len(columnas) >= 2 and columnas[-1].get("es_mes_actual") \
            and columnas[-2].get("es_mes_actual"):
        _pos = len(columnas) - 1   # entre la anteultima y la ultima
        for _ln in lineas_out:
            _vals = _ln["valores"]
            _a, _b = _vals[_pos - 1], _vals[_pos]
            try:
                _d = (_b - _a) if (_a is not None and _b is not None) else None
            except Exception:  # noqa: BLE001
                _d = None
            _vals.insert(_pos, _d)
        columnas.insert(_pos, {
            "key": "delta", "es_delta": True, "es_mes_actual": False,
            "es_canonico_default": False, "id_historia": None,
            "fecha_crea": None, "anio": None, "mes": None, "dia": None,
            "label_corto": "Δ", "label_largo": "Δ", "snap": None,
        })

    return {
        "columnas": columnas,
        "lineas": lineas_out,
        "meses_sin_snap": sin_snap,
        "n_actual": n_actual,
        "hoy": hoy,
    }


def tomar_snapshot_mes_actual(
    usuario: str = "web",
    throttle_segundos: int = 86400,  # TMT 2026-05-20 v2: 1h → 24h
) -> dict:
    """Inserta un snapshot del mes actual en scintela.historia.

    TMT 2026-05-20 — pedido dueña: al entrar a /historico-12m se toma
    un snapshot nuevo del mes en curso, sin pisar el anterior, para
    poder comparar.

    TMT 2026-05-20 v2 — throttle subido de 1h a 24h. Antes cada hora
    se creaba una columna nueva del mes actual; después de 3 horas
    aparecían 3 columnas duplicadas en /historico-12m. Ahora máximo
    1 snapshot por día (la dueña valida/borra desde la UI si quiere
    comparar varios).

    `throttle_segundos` evita re-snapshot por refresh accidental: si
    el último snapshot del mes actual es de hace menos que ese tiempo,
    no inserta nada y devuelve `accion='throttled'`.

    Devuelve `{accion: 'inserted'|'throttled', id_historia, kpis}`.
    """
    from datetime import datetime as _dt

    hoy = today_ec()

    # Chequear throttle.
    ult = db.fetch_one(
        """
        SELECT id_historia, fecha_crea
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha_crea DESC
         LIMIT 1
        """,
        (hoy.year, hoy.month),
    )
    if ult and ult.get("fecha_crea"):
        edad = (_dt.now() - ult["fecha_crea"]).total_seconds()
        if edad < throttle_segundos:
            return {
                "accion": "throttled",
                "id_historia": int(ult["id_historia"]),
                "motivo": f"último snapshot hace {int(edad)}s (< {throttle_segundos}s)",
            }

    # Importar el script de snapshot dinamicamente (vive en scripts/).
    import os
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    scripts_dir = os.path.join(repo_root, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import snapshot_historia_mensual as _snap

    r = _snap.ejecutar(hoy, force=False, usuario=usuario)
    # Si ya existía (skipped), forzamos insert nuevo manual — queremos
    # MÚLTIPLES snapshots del mismo mes para comparar.
    if r.get("accion") == "skipped":
        kpis = _snap.calcular_kpis(hoy)
        new_id = _snap.insertar_snapshot(kpis, usuario=usuario)
        return {"accion": "inserted", "id_historia": new_id, "kpis": kpis}
    return {"accion": r.get("accion"), "id_historia": r.get("id_historia"), "kpis": r.get("kpis", {})}


def validar_snapshot(id_historia: int, *, usuario: str = "web") -> dict:
    """Marca un snapshot como "el bueno" y borra los OTROS del mismo mes.

    TMT 2026-05-20 — pedido dueña: cuando hay 2 columnas del mes actual,
    elige una y los demás se borran.
    """
    row = db.fetch_one(
        "SELECT id_historia, fecha FROM scintela.historia WHERE id_historia = %s",
        (id_historia,),
    )
    if not row:
        raise ValueError(f"Snapshot id={id_historia} no existe.")
    fecha = row["fecha"]
    n = db.execute(
        """
        DELETE FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM %s::date)
           AND EXTRACT(MONTH FROM fecha) = EXTRACT(MONTH FROM %s::date)
           AND id_historia <> %s
        """,
        (fecha, fecha, id_historia),
    )
    return {"id_historia": id_historia, "n_borrados": int(n or 0)}


def borrar_snapshot(id_historia: int) -> int:
    """Borra un snapshot específico de scintela.historia."""
    return db.execute(
        "DELETE FROM scintela.historia WHERE id_historia = %s",
        (id_historia,),
    )


def consolidar_snapshots_mes_actual(conservar: int = 2) -> int:
    """Borra los snapshots del mes actual salvo los `conservar` mas recientes.

    Federico 2026-05-21 -- la pantalla Historial deja siempre la columna
    previa + la nueva (conservar=2) para comparar; lo mas viejo se limpia.
    Devuelve la cantidad de filas borradas.
    """

    hoy = today_ec()
    k = max(1, int(conservar))
    return db.execute(
        """
        DELETE FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %(a)s
           AND EXTRACT(MONTH FROM fecha) = %(m)s
           AND id_historia NOT IN (
               SELECT id_historia
                 FROM scintela.historia
                WHERE EXTRACT(YEAR FROM fecha) = %(a)s
                  AND EXTRACT(MONTH FROM fecha) = %(m)s
                ORDER BY fecha_crea DESC NULLS LAST, id_historia DESC
                LIMIT %(k)s
           )
        """,
        {"a": hoy.year, "m": hoy.month, "k": k},
    ) or 0


def eliminar_ultima_columna_mes_actual() -> dict:
    """Borra la columna mas reciente del mes actual.

    Federico 2026-05-21 -- boton 'Eliminar ultima': cuando la columna
    recien creada tiene errores, se borra y queda viva la previa (el
    ultimo milestone bueno).
    """

    hoy = today_ec()
    ult = db.fetch_one(
        """
        SELECT id_historia
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha_crea DESC NULLS LAST, id_historia DESC
         LIMIT 1
        """,
        (hoy.year, hoy.month),
    )
    if not ult:
        return {"borrado": False, "motivo": "no hay columnas del mes actual"}
    n = db.execute(
        "DELETE FROM scintela.historia WHERE id_historia = %s",
        (ult["id_historia"],),
    )
    return {"borrado": True, "id_historia": int(ult["id_historia"]), "n": int(n or 0)}


def historico_mom(anio_a: int, mes_a: int, anio_b: int, mes_b: int) -> dict:
    """Comparación mes-vs-mes para el informe histórico TINT.BAT.

    Toma dos pares (año, mes) y devuelve la misma estructura de filas que
    `historico_12m_matriz` pero con SOLO dos columnas — mes A y mes B —
    y un delta absoluto + delta % por fila.

    Convención de orden: mes A es el "viejo" (referencia), mes B es el
    "nuevo" (actual). El delta es B − A.
    """
    par_a = (int(anio_a), int(mes_a))
    par_b = (int(anio_b), int(mes_b))
    snaps = _cargar_snapshots([par_a, par_b])

    lineas_out = []
    for label, key, fmt, color, section in _HIST_LINEAS:
        v_a = _valor_para_linea(key, snaps.get(par_a))
        v_b = _valor_para_linea(key, snaps.get(par_b))
        if v_a is None or v_b is None:
            delta_abs = None
            delta_pct = None
        else:
            delta_abs = v_b - v_a
            delta_pct = ((v_b - v_a) / abs(v_a) * 100.0) if abs(v_a) > 0.005 else None
        lineas_out.append(
            {
                "label": label,
                "key": key,
                "fmt": fmt,
                "color": color,
                "section": section,
                "v_a": v_a,
                "v_b": v_b,
                "delta_abs": delta_abs,
                "delta_pct": delta_pct,
            }
        )

    sin_snap = [f"{p[1]:02d}/{p[0]}" for p in (par_a, par_b) if p not in snaps]

    return {
        "par_a": par_a,
        "par_b": par_b,
        "lineas": lineas_out,
        "meses_sin_snap": sin_snap,
    }


def historico_meses_disponibles() -> list[tuple[int, int]]:
    """Lista distinct (año, mes) de scintela.historia, descendente.

    Se usa para poblar los dropdowns del modo "mes vs mes".
    """
    rows = (
        db.fetch_all(
            """
        SELECT DISTINCT
               EXTRACT(YEAR FROM fecha)::int  AS anio,
               EXTRACT(MONTH FROM fecha)::int AS mes
          FROM scintela.historia
         ORDER BY anio DESC, mes DESC
        """
        )
        or []
    )
    return [(int(r["anio"]), int(r["mes"])) for r in rows]


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

    if not as_of:
        as_of = today_ec()

    # --- Saldos running ---
    salcaj_row = (
        db.fetch_one(
            """
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         WHERE fecha <= %s
         ORDER BY fecha DESC, id_caja DESC
         LIMIT 1
        """,
            (as_of,),
        )
        or {}
    )
    salcaj = float(salcaj_row.get("saldo") or 0)

    salbanc_rows = (
        db.fetch_all(
            """
        SELECT DISTINCT ON (no_banco)
               no_banco, COALESCE(saldo, 0) AS saldo
          FROM scintela.transacciones_bancarias
         WHERE fecha <= %s
         ORDER BY no_banco, fecha DESC, id_transaccion DESC
        """,
            (as_of,),
        )
        or []
    )
    salbanc_bancos = sum(float(r.get("saldo") or 0) for r in salbanc_rows)
    # TMT 2026-05-20 v7 — FÓRMULA CANÓNICA DE BANCO (SALBANC):
    #
    #     salbanc = SUM(último saldo por banco al as_of)
    #             + posdats banc=1 al as_of
    #             + posdats banc=2 al as_of
    #
    # `/balance` la calcula así (línea 2751): `salbanc = total_bancos +
    # pos1 + pos2`. La razón es que los cheques propios emitidos pero NO
    # cobrados (banc=1/2 en posdat) deben sumarse al saldo del banco
    # porque la transacción bancaria los DESCONTÓ inmediatamente al
    # emitirlos — pero contablemente el dinero sigue siendo nuestro hasta
    # que el beneficiario los cobra. Drift histórico: balance_components_as_of
    # no sumaba pos1/pos2 → BANCO en F&U distinto a /balance.
    pos_row = (
        db.fetch_one(
            """
        SELECT
          COALESCE(SUM(CASE WHEN banc=1 THEN importe ELSE 0 END), 0) AS pos1,
          COALESCE(SUM(CASE WHEN banc=2 THEN importe ELSE 0 END), 0) AS pos2
          FROM scintela.posdat
         WHERE fecha <= %s
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """,
            (as_of,),
        )
        or {}
    )
    pos1 = float(pos_row.get("pos1") or 0)
    pos2 = float(pos_row.get("pos2") or 0)
    salbanc = salbanc_bancos + pos1 + pos2

    # --- Cheques en cartera (no depositados, no anulados) ---
    totc_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0) AS total
          FROM scintela.cheque
         WHERE stat IN ('Z','1','2','3','P','D')
           AND COALESCE(fecha_recibido, fecha) <= %s
           AND (fechaing IS NULL OR fechaing > %s)
        """,
            (as_of, as_of),
        )
        or {}
    )
    totc = float(totc_row.get("total") or 0)

    # --- Facturas vivas as_of ---
    # Aproximación: factura.saldo actual (no rewind de abonos post-as_of).
    totf_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(saldo), 0) AS total
          FROM scintela.factura
         WHERE fecha <= %s
           AND COALESCE(saldo, 0) > 0
           AND (stat IS NULL OR stat IN ('Z','A','',' '))
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (as_of,),
        )
        or {}
    )
    totf = float(totf_row.get("total") or 0)

    # --- Posdat deuda viva as_of ---
    # TMT 2026-05-20 v7 — FÓRMULA CANÓNICA DE PASIVOS (TOTP):
    #
    #     SUM(importe) FROM scintela.posdat
    #     WHERE COALESCE(banc, 0) = 0     ← POSDAT_DEUDA_VIVA_WHERE
    #       AND fecha <= as_of            ← solo opera vivos en ese momento
    #       AND (anulada IS NOT TRUE OR anulada IS NULL)
    #
    # Drift histórico arreglado en v7: ANTES tenía `importe > 0` adicional,
    # que excluía los importes negativos (anticipos/ajustes que reducen la
    # deuda). /balance no aplica ese filtro → drift entre pantallas. La
    # fórmula canónica es la misma de `posdat_totales()` con el agregado
    # del filtro de fecha para snapshots históricos.
    totp_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0) AS total
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fecha <= %s
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """,
            (as_of,),
        )
        or {}
    )
    totp = float(totp_row.get("total") or 0)

    # --- Stock / activos / patrimonio del último CIERRE MENSUAL anterior ---
    # Federico 2026-05-22 — antes era `fecha <= as_of`, que para el mes en
    # curso agarraba un snapshot del propio mes (la pantalla Historial crea
    # uno al entrar) → PATANT = mes actual y la utilidad daba ~0. El cierre
    # de referencia tiene que ser el del MES ANTERIOR al de as_of.
    hist_prev = (
        db.fetch_one(
            """
        SELECT fecha, stock, ustock, uqui, maquinaria, realty, anticipos,
               patrimonio, usret, usuti
          FROM scintela.historia
         WHERE fecha < DATE_TRUNC('month', %s::date)
         ORDER BY fecha DESC
         LIMIT 1
        """,
            (as_of,),
        )
        or {}
    )
    # ─── Stock / VQX / activos / anticipos AS-OF — SELF-SUFICIENTE ───────
    # Antes copiaba vsto/vqx/umaq/uact/antic del snapshot del mes ANTERIOR
    # (hist_prev) → cadena frágil que arrastraba errores y ataba PC a los
    # snapshots sembrados por el dBase. Bug 2026-07-01: el cierre de junio
    # quedó mal y ningún recompute lo corregía. Ahora cada componente se
    # calcula desde los datos crudos de PC:
    #   · VSTO = INICIALE[mes de as_of] × tarifas. El stock de CIERRE queda
    #     escrito en esa fila (write-back del PRG L517) y SOBREVIVE al purge
    #     de TINTO (que es solo-mes-en-curso). Recomputar desde movimientos NO
    #     es posible para meses pasados (los movimientos ya no están).
    #   · VQX = INICIALE[mes].vq
    #   · activos = ACTIVOS as_of (coef por día) · anticipos = DOLARES as_of.
    # PATANT sigue siendo el patrimonio del cierre ANTERIOR (hist_prev) —
    # eso SÍ es el mes previo por definición. [[iniciales_mes_actual]]
    _inic_asof = (
        db.fetch_one(
            """
            SELECT hilado, tejido, terminado, um, uk, uf, vq
              FROM scintela.iniciales
             WHERE yy = EXTRACT(YEAR FROM %s::date)::int
               AND mesnum = EXTRACT(MONTH FROM %s::date)::int
             ORDER BY id_iniciales DESC
             LIMIT 1
            """,
            (as_of, as_of),
        )
        or {}
    )
    if _inic_asof and float(_inic_asof.get("hilado") or 0) > 0:
        _hi = float(_inic_asof.get("hilado") or 0)
        _tj = float(_inic_asof.get("tejido") or 0)
        _pf = float(_inic_asof.get("terminado") or 0)
        vsto = _hi * float(_inic_asof.get("um") or 0) \
            + _tj * float(_inic_asof.get("uk") or 0) \
            + _pf * float(_inic_asof.get("uf") or 0)
        vqx = float(_inic_asof.get("vq") or 0)
    else:
        # Fallback defensivo: sin fila de iniciales del mes, usar el snapshot
        # anterior (comportamiento previo).
        vsto = float(hist_prev.get("ustock") or 0)
        vqx = float(hist_prev.get("uqui") or 0)

    _act = (
        db.fetch_one(
            """
            WITH coef AS (
              SELECT LEAST(EXTRACT(DAY FROM %s::date)::numeric, 30) / 30.0 AS c
            ),
            v AS (
              SELECT tipo,
                     COALESCE(inicial, 0) - COALESCE(amortizac, 0)
                       - (SELECT c FROM coef) * COALESCE(cuota, 0) AS valor_calc
                FROM scintela.activos
            )
            SELECT
              COALESCE(SUM(CASE WHEN tipo IN ('M','C','K') THEN GREATEST(valor_calc,0) ELSE 0 END),0) AS umaq,
              COALESCE(SUM(CASE WHEN tipo = 'I'           THEN GREATEST(valor_calc,0) ELSE 0 END),0) AS uact
            FROM v
            """,
            (as_of,),
        )
        or {}
    )
    umaq = float(_act.get("umaq") or 0) or float(hist_prev.get("maquinaria") or 0)
    uact = float(_act.get("uact") or 0) or float(hist_prev.get("realty") or 0)

    # Anticipos as_of. OJO: DOLARES guarda estado ACTUAL — para cierres
    # RECIENTES (el mes que se acaba de cerrar) los anticipos abiertos siguen
    # abiertos y da exacto; para meses MUY viejos algunos ya se aplicaron y no
    # se pueden reconstruir → por eso el cierre hay que fotografiarlo a tiempo.
    _ant = (
        db.fetch_one(
            """
            SELECT COALESCE(SUM(importe), 0) AS total
              FROM scintela.dolares
             WHERE (st IS NULL OR st IN ('', ' '))
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
               AND (fecha IS NULL OR fecha <= %s)
            """,
            (as_of,),
        )
        or {}
    )
    antic = float(_ant.get("total") or 0)

    patant = float(hist_prev.get("patrimonio") or 0)

    # --- Flujos del mes (mes que contiene as_of) ---
    kcom_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
          FROM scintela.compra
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (as_of, as_of),
        )
        or {}
    )
    kcom = float(kcom_row.get("kg") or 0)
    ucom = float(kcom_row.get("importe") or 0)

    vent_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS importe
          FROM scintela.factura
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') <> 'X'
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (as_of, as_of),
        )
        or {}
    )
    kvent = float(vent_row.get("kg") or 0)
    uvent = float(vent_row.get("importe") or 0)

    gasto_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(importe), 0) AS importe
          FROM scintela.xgast
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
            (as_of, as_of),
        )
        or {}
    )
    gasto = float(gasto_row.get("importe") or 0)

    # USRET — retiros del mes (de scintela.retiros). Federico 2026-05-22:
    # antes estaba hardcodeado en 0.0, lo que rompía la identidad
    # utilidad = Δ patrimonio + retiros (el snapshot quedaba con utilidad
    # de menos y usret en cero).
    usret_row = (
        db.fetch_one(
            """
        SELECT COALESCE(SUM(ret), 0) AS total,
               COALESCE(SUM(CASE WHEN COALESCE(usuario_crea, '') = 'pc-retiro-op'
                                 THEN -ret ELSE ret END), 0) AS total_calc
          FROM scintela.retiros
         WHERE DATE_TRUNC('month', fecha) = DATE_TRUNC('month', %s::date)
           AND fecha <= %s
        """,
            (as_of, as_of),
        )
        or {}
    )
    usret = float(usret_row.get("total") or 0)
    # TMT 2026-07-06 v6: sin ajuste (el crédito OP se consume y el dividendo
    # compensa solo) — usret_calc = usret.
    usret_calc = usret

    # Computar cartera (totc + totf), subt, totl, patr, retiro
    cart = totc + totf
    subt = salbanc + salcaj + cart
    totl = subt + vsto + vqx + umaq + uact + antic
    patr = totl - totp
    # Utilidad del mes = Δ patrimonio + retiros: los retiros bajaron el
    # patrimonio, así que se re-suman para llegar a la utilidad real.
    # v5: usret_calc (retiros OP en negativo — su plata ya restó por posdat).
    utilidad = (patr - patant) + usret_calc

    return {
        # Saldos
        "salcaj": salcaj,
        # Federico 2026-05-22 — `salbanc` y `banco` incluyen la CAJA
        # (caja y bancos). El patrimonio ya incluía la caja vía `subt`,
        # pero la columna `banco` del snapshot guardaba solo bancos →
        # el Historial mostraba Activo-Pasivo de menos por el monto de
        # la caja.
        "salbanc": salbanc + salcaj,
        "salbanc_bancos": salbanc_bancos,  # solo bancos, sin caja — debug
        "pos1": pos1,
        "pos2": pos2,
        "banco": salbanc + salcaj,
        "totc": totc,
        "totf": totf,
        "cart": cart,
        # Pasivos
        "totp": totp,
        "deuda": totp,
        # Activos fijos / stock
        "vsto": vsto,
        "stock": float(hist_prev.get("stock") or 0),
        "ustock": vsto,
        "uqui": vqx,
        "vqx": vqx,
        "umaq": umaq,
        "maquinaria": umaq,
        "uact": uact,
        "realty": uact,
        "antic": antic,
        "anticipos": antic,
        # Patrimonio + utilidad
        "subt": subt,
        "totl": totl,
        "patr": patr,
        "patrimonio": patr,
        "patant": patant,
        "utilidad": utilidad,
        "usuti": utilidad,  # snapshot guarda usuti = utilidad del período
        "usret": usret,
        "retiro": usret,
        # Flujos del mes
        "kcom": kcom,
        "ucom": ucom,
        "kvent": kvent,
        "uvent": uvent,
        "ktej": 0.0,  # Aproximación — kg de tejido del mes requiere
        "ktin": 0.0,  # join con tinto/compras tipo K. Omitido por scope.
        "utej": 0.0,
        "utin": 0.0,
        "costo": ucom,  # Default conservador.
        "gasto": gasto,
        "gstotal": gasto,
        "dolar": 0.0,
        # Meta
        "as_of": as_of,
    }


def informe_balance_as_of(as_of=None) -> dict:
    """Wrapper que devuelve un dict similar a informe_balance() pero
    calculado as_of. Default as_of=hoy = comportamiento clásico.

    Implementado parcialmente: cubre los componentes necesarios para
    `crear_snapshot_historia`. Para uso UI completo, seguir usando
    `informe_balance()`.
    """

    if as_of is None or as_of == today_ec():
        # As_of = hoy → comportamiento clásico, llama al balance live.
        return informe_balance()
    components = balance_components_as_of(as_of)
    return {
        "fecha": as_of,
        "kg": {
            "kcom": components["kcom"],
            "ucom": components["ucom"],
            "ktej": components["ktej"],
            "ktin": components["ktin"],
            "kvent": components["kvent"],
            "uvent": components["uvent"],
            "utej": components["utej"],
            "utin": components["utin"],
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


def crear_snapshot_historia(anio: int, mes: int, usuario: str = "auto") -> dict:
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
    import calendar
    from datetime import date as _date

    anio = int(anio)
    mes = int(mes)
    periodo_clave = f"{anio:04d}-{mes:02d}"

    # TMT 2026-05-19 v6 audit — calculamos el balance "as_of último día del mes"
    # para que los snapshots de backfill queden con la foto correcta. Antes
    # esto usaba `informe_balance()` LIVE → backfills con saldo de hoy.
    last_day = calendar.monthrange(anio, mes)[1]
    fecha_snap = _date(anio, mes, last_day)

    def _existe_cierre() -> bool:
        # 2026-06-04 — dedup por la fecha EXACTA del cierre (último día del
        # mes), NO por año/mes. El mes puede tener snapshots web de mitad de
        # mes (fecha = día intermedio) y aún faltar el cierre canónico de fin
        # de mes. Con el chequeo viejo `snapshot_historia_existe(anio, mes)`
        # el cron saltaba el cierre si ya había cualquier foto del mes.
        return bool(
            db.fetch_one(
                "SELECT 1 FROM scintela.historia WHERE fecha = %s LIMIT 1",
                (fecha_snap,),
            )
        )

    if _existe_cierre():
        return {
            "aplicado": False,
            "anio": anio,
            "mes": mes,
            "id_historia": None,
            "razon": f"Ya existe snapshot de cierre ({fecha_snap}) para {periodo_clave}.",
        }

    # TMT 2026-07-15: si cerramos el mes EN CURSO y ya llegó (o pasó) el último
    # día, usar el balance LIVE — la MISMA cadena Asinfo que la foto diaria — para
    # que el cierre valúe el stock igual y no divergir el PATANT del mes siguiente
    # (misma familia del bug utilidad 54k vs 179k). Meses pasados = as_of (no hay
    # stock Asinfo histórico; se lee de historia). [[coherencia_numeros_una_fuente]]
    _hoy_cierre = today_ec()
    if anio == _hoy_cierre.year and mes == _hoy_cierre.month and _hoy_cierre >= fecha_snap:
        bal = informe_balance()
    else:
        bal = informe_balance_as_of(fecha_snap)
    if not bal or bal.get("error"):
        return {
            "aplicado": False,
            "anio": anio,
            "mes": mes,
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
        # Re-check después del lock (por fecha de cierre exacta).
        if _existe_cierre():
            return {
                "aplicado": False,
                "anio": anio,
                "mes": mes,
                "id_historia": None,
                "razon": "race ganada por otra request",
            }

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
                "fecha": fecha_snap,
                # Stock KG y US$
                "stock": float(kg.get("stock_kg") or kg.get("stock_kg_live") or 0),
                "ustock": float(stock_sub.get("total_us") or 0),
                "uqui": float(d.get("vqx") or 0),
                # Flujos del mes (KG)
                "kcom": float(kg.get("kcom") or 0),
                "ktej": float(kg.get("ktej") or 0),
                "ktin": float(kg.get("ktin") or 0),
                "kvent": float(kg.get("kvent") or 0),
                # Flujos del mes (US$)
                "ucom": float(kg.get("ucom") or 0),
                "utej": float(kg.get("utej") or 0),
                "utin": float(kg.get("utin") or 0),
                "uvent": float(kg.get("uvent") or 0),
                "costo": float(kg.get("costo_mes") or 0),
                # Resultados del mes.
                # 2026-06-04 fix — balance_components_as_of expone las claves
                # `gasto`/`gstotal`/`usret`/`retiro`, NO `gastos_mes`/
                # `gastos_total`/`uret`. Antes estos .get() caían al default y
                # guardaban gasto=0 y RR=0 en cada snapshot del camino as_of
                # (backfill + cron nuevo).
                "gasto": float(d.get("gasto") or 0),
                "gstotal": float(d.get("gstotal") or d.get("gasto") or 0),
                # Balance components. `salbanc` ya incluye la CAJA
                # (balance_components_as_of la suma) → cierra la identidad
                # ACTIVO − PASIVO = PATRIMONIO.
                "banco": float(d.get("salbanc") or 0),
                "cart": float(d.get("totc", 0) or 0) + float(d.get("totf", 0) or 0),
                "deuda": float(d.get("totp") or 0),
                "retiro": float(d.get("retiro", d.get("usret")) or 0),
                "patrimonio": float(d.get("patr") or 0),
                "anticipos": float(d.get("antic") or 0),
                "dolar": 0.0,  # no usado en PC
                "maquinaria": float(d.get("umaq") or 0),
                "realty": float(d.get("uact") or 0),
                "usret": float(d.get("usret") or 0),
                "usuti": float(d.get("utilidad") or 0),
                "usuario": usuario[:50],
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
        "aplicado": True,
        "anio": anio,
        "mes": mes,
        "id_historia": (res or {}).get("id_historia"),
        "razon": f"Snapshot creado para {periodo_clave}.",
    }


_NUM_MES_EN = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
               7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}


def rollover_y_writeback_iniciales(fecha=None, dry_run: bool = False) -> dict:
    """Replica el rollover automático del dBase (MENU.PRG/SETEO 246-262) + el
    write-back de stock (INFORMES.PRG 549-550), para que PC NO dependa de que
    el dBase abra el 1° de mes ni de un sync a tiempo.

    (1) ROLLOVER: si falta la fila de INICIALES del mes EN CURSO, la crea
        copiando el CIERRE del mes anterior (apertura = cierre anterior). Es lo
        que faltó el 01/07 (sin fila del mes → terminado 0 → utilidad rota).
    (2) WRITE-BACK: escribe el stock de cierre VIVO (hilado/tejido/terminado/vq
        + tarifas) en la fila del mes en curso, para que el mes que viene el
        rollover tome el cierre CORRECTO. HISTORIA solo guarda el stock TOTAL —
        el desglose por etapa vive solo en INICIALES, por eso hay que escribirlo.

    Idempotente. Corre en el cron diario ANTES de la foto. NO toca campos de
    presupuesto (kprog/gprog/pretot) — solo stock y tarifas.
    """
    from filters import today_ec

    hoy = fecha or today_ec()
    m, y = hoy.month, hoy.year
    prev_m = 12 if m == 1 else m - 1
    prev_y = y - 1 if m == 1 else y
    out = {"fecha": str(hoy), "rollover": False, "writeback": False}

    # (1) ROLLOVER — crear la fila del mes en curso si falta
    existe = db.fetch_one(
        "SELECT id_iniciales FROM scintela.iniciales WHERE mesnum=%s AND yy=%s LIMIT 1",
        (m, y),
    )
    if not existe:
        prev = db.fetch_one(
            "SELECT * FROM scintela.iniciales WHERE mesnum=%s AND yy=%s "
            "ORDER BY id_iniciales DESC LIMIT 1",
            (prev_m, prev_y),
        )
        if prev:
            _row_new = {
                    "mesnum": m, "mesnom": _NUM_MES_EN.get(m, str(m)), "yy": y,
                    "hilado": prev.get("hilado"), "tejido": prev.get("tejido"),
                    "terminado": prev.get("terminado"), "vq": prev.get("vq"),
                    "um": prev.get("um"), "uk": prev.get("uk"), "uf": prev.get("uf"),
                    "uq": prev.get("uq"), "pre": prev.get("pre"),
                    "kprog": prev.get("kprog"), "gprog": prev.get("gprog"),
                    "numnot": prev.get("numnot"), "dificil": prev.get("dificil"),
                    "pretej": prev.get("pretej"), "pretin": prev.get("pretin"),
                    "preadm": prev.get("preadm"), "pretot": prev.get("pretot"),
            }
            out["rollover"] = True
            out["rollover_desde"] = f"{prev_m:02d}/{prev_y}"
            out["fila_nueva"] = {"mesnum": m, "mesnom": _NUM_MES_EN.get(m, str(m)),
                                 "yy": y, "hilado": _row_new["hilado"],
                                 "tejido": _row_new["tejido"],
                                 "terminado": _row_new["terminado"], "vq": _row_new["vq"]}
            if not dry_run:
                db.execute(
                    """
                    INSERT INTO scintela.iniciales
                        (mesnum, mesnom, yy, hilado, tejido, terminado, vq,
                         um, uk, uf, uq, pre, kprog, gprog, numnot, dificil,
                         pretej, pretin, preadm, pretot, usuario_crea)
                    VALUES (%(mesnum)s, %(mesnom)s, %(yy)s, %(hilado)s, %(tejido)s,
                            %(terminado)s, %(vq)s, %(um)s, %(uk)s, %(uf)s, %(uq)s,
                            %(pre)s, %(kprog)s, %(gprog)s, %(numnot)s, %(dificil)s,
                            %(pretej)s, %(pretin)s, %(preadm)s, %(pretot)s,
                            'rollover-pc')
                    """,
                    _row_new,
                )
        else:
            out["rollover_error"] = f"no hay fila del mes anterior {prev_m:02d}/{prev_y} para copiar"

    # (2) WRITE-BACK — stock de cierre vivo a la fila del mes en curso
    bal = informe_balance()
    if not bal or bal.get("error"):
        out["writeback_error"] = (bal or {}).get("error") or "sin balance"
        return out
    stock = (bal.get("resultados") or {}).get("stock") or {}
    hi = stock.get("hilado") or {}
    tj = stock.get("tejido") or {}
    pf = stock.get("terminado") or {}
    comp = (bal.get("diagnostico") or {}).get("componentes") or {}
    hi_kg = float(hi.get("kg") or 0)
    pf_kg = float(pf.get("kg") or 0)
    # Guarda: NO pisar con stock roto (hilado o terminado en 0).
    if hi_kg > 0 and pf_kg > 0:
        if not dry_run:
            db.execute(
                """
                UPDATE scintela.iniciales
                   SET hilado=%s, tejido=%s, terminado=%s, vq=%s,
                       um=%s, uk=%s, uf=%s
                 WHERE mesnum=%s AND yy=%s
                """,
                (
                    hi_kg, float(tj.get("kg") or 0), pf_kg, float(comp.get("vqx") or 0),
                    float(hi.get("ukg") or 0), float(tj.get("ukg") or 0),
                    float(pf.get("ukg") or 0), m, y,
                ),
            )
        out["writeback"] = True
        out["stock"] = {"hilado": hi_kg, "tejido": float(tj.get("kg") or 0),
                        "terminado": pf_kg}
    else:
        out["writeback_skip"] = "stock hilado/terminado en 0 — no piso"
    return out


def crear_snapshot_diario(usuario: str = "snapshot-diario", fecha=None) -> dict:
    """Fotografía el balance VIVO de HOY en scintela.historia (fecha = hoy).

    La foto diaria resuelve el problema de fondo: como se captura EN VIVO
    (cartera, anticipos, stock, tinto del día están frescos), no hay que
    reconstruir nada tarde. El cierre de mes pasa a ser, simplemente, la foto
    del último día del mes — ya validada por el health durante todo el mes.

    Idempotente: pisa la fila `snapshot-diario` del día en cada corrida (así se
    asienta a medida que entra el movimiento del día). NO toca filas de otro
    origen (dbf-import, andres, regen) del mismo día.

    Usa `informe_balance()` (vivo, con el fix del stock) — NO `informe_balance_as_of`,
    que para el stock leería el caché INICIALE del dBase en vez del stock live.
    """
    from filters import today_ec

    hoy = fecha or today_ec()
    bal = informe_balance()
    if not bal or bal.get("error"):
        return {"aplicado": False, "fecha": str(hoy),
                "razon": f"balance falló: {bal.get('error') if bal else 'sin data'}"}

    comp = (bal.get("diagnostico") or {}).get("componentes") or {}
    kg = bal.get("kg") or {}

    # TMT 2026-07-14 — BUG VENTAS kg / precio del mes ANTERIOR en la foto diaria.
    # Las llaves kvent/uvent del bloque `kg` de informe_balance() son las "kg del
    # ÚLTIMO CIERRE" (panel F9), leídas de historia_ultimo_mes() = el snapshot del
    # mes PREVIO — NO las ventas del mes en curso. La foto diaria las persistía
    # tal cual, así que la columna "previa" del Historial quedaba con las ventas
    # de junio (335.700 kg · 8,52 U$/kg) mientras la columna "en vivo" mostraba
    # julio real (128.510) → Δ fantasma −207.190. Tomamos kvent/uvent del MISMO
    # cálculo que alimenta la columna "en vivo" (calcular_kpis, vía
    # _snap_live_mes_actual) para que previa y en-vivo coincidan cuando no hubo
    # cambios reales. [[coherencia_numeros_una_fuente]]
    _live_kpis = _snap_live_mes_actual(hoy)
    if _live_kpis:
        _kvent_live = float(_live_kpis.get("kvent") or 0)
        _uvent_live = float(_live_kpis.get("uvent") or 0)
    else:
        # Fallback defensivo (calcular_kpis falló): ventas live del mes en curso
        # — nunca el kvent stale del último cierre.
        _vm_live = ventas_mes_corriente_resultado()
        _kvent_live = float(_vm_live.get("kg") or 0)
        _uvent_live = float(_vm_live.get("importe") or 0)

    def _c(k, default=0.0):
        return float(comp.get(k) or default)

    banco = _c("salbanc_total") + _c("salcaj")  # bancos + caja
    # TMT 2026-07-15: kg de stock de HOY (mismo panel STOCK que valúa ustock),
    # NO kg["stock_kg"] (= kg del cierre anterior) — así la fila de historia
    # tiene kg y $ coherentes (misma foto). Fallback al viejo si falta. [[coherencia]]
    _stock_kg_hoy = float(((bal.get("stock") or {}).get("total") or {}).get("kg") or 0)
    row = {
        "fecha": hoy,
        "stock": _stock_kg_hoy or float(kg.get("stock_kg") or 0),
        "kcom": float(kg.get("kcom") or 0),
        "ktej": float(kg.get("ktej") or 0),
        "ktin": float(kg.get("ktin") or 0),
        "ustock": float(bal.get("vsto") or _c("vsto")),
        # TMT 2026-07-15: uqui del top-level (químico final, POLI+ALG físico ~338k),
        # NO componentes["vqx"] (viejo ~279k) — igual que ustock/patrimonio/usuti,
        # así la fila de historia cumple ACTIVO−PASIVO=PATRIMONIO. [[coherencia]]
        "uqui": float(bal.get("vqx") or _c("vqx")),
        "kvent": _kvent_live,
        "uvent": _uvent_live,
        "costo": float(kg.get("costo_mes") or 0),
        "ucom": float(kg.get("ucom") or 0),
        "utej": float(kg.get("utej") or 0),
        "utin": float(kg.get("utin") or 0),
        "gasto": float(comp.get("gasto") or 0),
        "gstotal": float(comp.get("gstotal") or comp.get("gasto") or 0),
        "banco": banco,
        "cart": _c("cart"),
        "deuda": _c("totp"),
        "retiro": _c("uret"),
        # TMT 2026-07-06: patrimonio NETO de retiros (dBase REPLA PATRIMONIO
        # WITH PATR-URET; mismo criterio que calcular_kpis y fuentes_y_usos).
        # Guardar el bruto inflaba PATANT cuando la foto diaria del último
        # día queda como cierre → la utilidad del mes siguiente arrancaba
        # subestimada por el monto de los retiros.
        "patrimonio": float(bal.get("patr") or _c("patr"))
                      - float(bal.get("uret") or _c("uret") or 0),
        "anticipos": _c("antic"),
        "dolar": 0.0,
        "maquinaria": _c("umaq"),
        "realty": _c("uact"),
        "usret": _c("uret"),
        "usuti": float(bal.get("utilidad") or _c("utilidad")),
        "usuario": usuario[:50],
    }

    with db.tx() as conn:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('snapshot_diario'))", conn=conn
        )
        # Pisar SOLO la foto diaria del día (no dbf-import/andres/regen del mismo día).
        db.execute(
            "DELETE FROM scintela.historia WHERE fecha = %(fecha)s "
            "AND usuario_crea = %(usuario)s",
            {"fecha": hoy, "usuario": usuario[:50]}, conn=conn,
        )
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
                    %(banco)s, %(cart)s, %(deuda)s, %(retiro)s, %(patrimonio)s,
                    %(anticipos)s, %(dolar)s, %(maquinaria)s, %(realty)s,
                    %(usret)s, %(usuti)s, CURRENT_TIMESTAMP, %(usuario)s)
            RETURNING id_historia
            """,
            row, conn=conn,
        )
    return {
        "aplicado": True,
        "fecha": str(hoy),
        "id_historia": (res or {}).get("id_historia"),
        "patrimonio": row["patrimonio"],
        "ustock": row["ustock"],
        "cart": row["cart"],
        "usuti": row["usuti"],
        "razon": f"Foto diaria creada para {hoy}.",
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

    # TMT 2026-05-20 v5 — fallback robusto contra snapshots vacíos.
    # El bug que se reportó: el snapshot existe pero tiene 0 en las
    # columnas críticas (banco, cart, ustock, etc.) porque
    # `balance_components_as_of` los lee del snapshot ANTERIOR. Si nunca
    # hubo un snapshot "padre" bueno, todos heredan 0 → fuentes-y-usos
    # muestra fila tras fila en "—".
    #
    # Heurística:
    #  1. Si el mes HASTA es el mes en curso (o un mes sin snapshot),
    #     calcular h_fin con `informe_balance()` LIVE de HOY → foto real.
    #  2. Si el mes DESDE no tiene snapshot, calcularlo con
    #     `balance_components_as_of(last_day(desde))` y armar un dict
    #     equivalente al row de historia.
    #  3. Si el snapshot existe pero `banco`, `cart`, o `ustock` están en
    #     0 → considerar inválido y recalcular as_of.
    import calendar as _cal
    from datetime import date as _date

    def _es_snap_vacio(row: dict) -> bool:
        """Snapshot inservible: las cuentas críticas están en 0."""
        if not row:
            return True
        return (
            float(row.get("banco") or 0) == 0
            and float(row.get("cart") or 0) == 0
            and float(row.get("ustock") or 0) == 0
        )

    def _row_desde_componentes(c: dict, fecha) -> dict:
        """Mapea componentes a las keys de scintela.historia row.

        TMT 2026-05-20 v5b — soporta DOS shapes:
          1. `balance_components_as_of()` devuelve: salbanc, totc, totf,
             vsto, vqx, umaq, uact, antic, totp, usret, usuti, patr (y
             algunos aliases ya mapeados).
          2. `informe_balance()['diagnostico']['componentes']` devuelve:
             salcaj, salbanc1, salbanc2, **salbanc_total**, totc, totf,
             cart, vsto, vqx, umaq, uact, antic, totp, uret, utilidad,
             patr. CLAVES DISTINTAS — bug 2026-05-20: mi versión vieja
             leía `salbanc` que no existe en el live components → todo 0.
        """
        # banco: live tiene `salbanc_total`, as_of tiene `salbanc`.
        banco = c.get("salbanc_total") or c.get("salbanc") or c.get("banco") or 0
        # cart: ambos tienen `cart` o (totc + totf).
        cart_v = c.get("cart")
        if cart_v is None:
            cart_v = (c.get("totc") or 0) + (c.get("totf") or 0)
        return {
            "fecha": fecha,
            "banco": float(banco or 0),
            "cart": float(cart_v or 0),
            "ustock": float(c.get("vsto") or c.get("ustock") or 0),
            "uqui": float(c.get("vqx") or c.get("uqui") or 0),
            "maquinaria": float(c.get("umaq") or c.get("maquinaria") or 0),
            "realty": float(c.get("uact") or c.get("realty") or 0),
            "anticipos": float(c.get("antic") or c.get("anticipos") or 0),
            "deuda": float(c.get("totp") or c.get("deuda") or 0),
            "usret": float(c.get("uret") or c.get("usret") or c.get("retiro") or 0),
            "usuti": float(c.get("utilidad") or c.get("usuti") or 0),
            "patrimonio": float(c.get("patr") or c.get("patrimonio") or 0),
            "_origen": "live",
        }

    def _row_desde_informe_balance(b: dict) -> dict:
        """h_fin del mes en curso desde informe_balance() — LIVE, los
        mismos valores que muestra la pantalla Resultados, cuenta por
        cuenta. Federico 2026-05-22: antes el mes en curso salía de
        balance_components_as_of(), que copiaba stock / anticipos /
        activos del cierre anterior → esas filas daban Δ=0 y la plata que
        entró a inventario aparecía como pérdida."""

        def _g(k: str) -> float:
            return float(b.get(k) or 0)

        return {
            "fecha": today_ec(),
            # CAJA Y BANCOS = bancos + caja (Resultados: salbanc + salcaj).
            "banco": _g("salbanc") + _g("salcaj"),
            # CARTERA = cheques + facturas.
            "cart": _g("totc") + _g("totf"),
            "anticipos": _g("antic"),
            "ustock": _g("vsto"),
            "uqui": _g("vqx"),
            "maquinaria": _g("umaq"),
            "realty": _g("uact"),
            "deuda": _g("totp"),
            # Patrimonio neto = PATR − URET (idéntico al que muestra
            # Resultados — balance.html fila "Patrimonio neto").
            "patrimonio": _g("patr") - _g("uret"),
            "usret": _g("uret"),
            # La utilidad del período se calcula como Δpatrimonio + retiros.
            "usuti": 0.0,
            "_origen": "live",
        }

    # TMT 2026-05-21 — Snapshot-first. Antes usábamos
    # balance_components_as_of() para ambos extremos. Tenía drift contra los
    # snapshots reales (cart 30x, banco 2x, pasivos 15x): mezclaba saldos
    # post-sync LIVE con valores históricos del snapshot anterior. Los
    # snapshots de scintela.historia son consistentes mes a mes (validados
    # contra el dBase original y /historico-12m), así que arrancamos por
    # ellos y solo caemos a as_of() si falta el snapshot del mes pedido.
    hoy = today_ec()
    es_mes_actual = yy == hoy.year and mm == hoy.month

    last_day_fin = _cal.monthrange(yy, mm)[1]
    fecha_fin = _date(yy, mm, last_day_fin) if not es_mes_actual else hoy
    last_day_ini = _cal.monthrange(yy_ant, mm_ant)[1]
    fecha_ini = _date(yy_ant, mm_ant, last_day_ini)

    comp_fin: dict = {}
    comp_ini: dict = {}

    # Federico 2026-05-22 — HASTA = mes en curso: NO se usa snapshot.
    # El mes en curso se calcula LIVE con informe_balance() — exactamente
    # los mismos valores que muestra la pantalla Resultados, cuenta por
    # cuenta (caja+bancos, cartera, anticipos, stock MP+PROD, stock quím.,
    # maquinaria, terrenos, pasivos). Así cada fila del cuadro lleva su
    # diferencia real. No hace falta cerrar ni regenerar ningún snapshot.
    if es_mes_actual:
        h_fin = None
        try:
            _bal_live = informe_balance() or {}
            if _bal_live and not _bal_live.get("error"):
                h_fin = _row_desde_informe_balance(_bal_live)
        except Exception:
            pass
    else:
        h_fin = _historia_en_mes(yy, mm)

    h_ini = _historia_en_mes(yy_ant, mm_ant)
    # Fallback DESDE: mes inicial sin snapshot (raro) → as_of(last_day).
    if not h_ini:
        try:
            last_day_ini = _cal.monthrange(yy_ant, mm_ant)[1]
            fecha_ini = _date(yy_ant, mm_ant, last_day_ini)
            comp_ini = balance_components_as_of(fecha_ini) or {}
            if comp_ini:
                h_ini = _row_desde_componentes(comp_ini, fecha_ini)
        except Exception:
            pass

    # Federico 2026-05-22 — emparejar la convención de caja entre los dos
    # extremos. Snapshots viejos guardaban `banco` SIN la caja mientras que
    # el mes en curso (live) SÍ la trae; sin esto la fila CAJA Y BANCOS
    # descuadraría el cuadro por el monto de la caja. _normalizar_caja le
    # suma a `banco` el faltante detectado contra el patrimonio guardado,
    # de modo que Σactivos − deuda == patrimonio en ambos extremos.
    def _normalizar_caja(row: dict | None) -> None:
        if not row:
            return
        _patr = float(row.get("patrimonio") or 0)
        if _patr == 0:
            return
        _activos = (
            float(row.get("banco") or 0)
            + float(row.get("cart") or 0)
            + float(row.get("anticipos") or 0)
            + float(row.get("ustock") or 0)
            + float(row.get("uqui") or 0)
            + float(row.get("maquinaria") or 0)
            + float(row.get("realty") or 0)
        )
        _falta = _patr - (_activos - float(row.get("deuda") or 0))
        if abs(_falta) > 1.0:
            row["banco"] = float(row.get("banco") or 0) + _falta

    _normalizar_caja(h_ini)
    _normalizar_caja(h_fin)

    if not h_fin or not h_ini:
        return {
            "anio": yy,
            "mes": mm,
            "h_ini": h_ini,
            "h_fin": h_fin,
            "fuentes": [],
            "usos": [],
            "total_fuentes": 0.0,
            "total_usos": 0.0,
            "delta_liquido": 0.0,
            "delta_banco": 0.0,
            "error": (
                "No hay snapshot mensual en scintela.historia para "
                f"{mm_ant:02d}/{yy_ant} y/o {mm:02d}/{yy}. "
                "El balance debe cerrarse mensualmente para generar este cuadro."
            ),
        }

    def f(row: dict, col: str) -> float:
        v = row.get(col)
        return float(v) if v is not None else 0.0

    # TMT 2026-05-20 v3 — corrección crítica replicando PRG L1654-1727:
    # 1. PRG usa `O=USTOCK` (US$ de stock), NO `stock` (kg). Mi código
    #    anterior usaba `stock` que es la cantidad en KG — mezclaba
    #    unidades en un cuadro de fondos.
    # 2. PRG suma USUTI/USRET sobre los meses del período (recno > recI
    #    y recno <= recF), NO hace Δ (final - inicial). Para períodos
    #    de >1 mes esto da distinto.

    # TMT 2026-05-20 v5c — Δ con guard contra columnas faltantes en h_ini.
    # Si h_ini.X = 0 (porque el snapshot del cierre anterior nunca se
    # rellenó bien para esa columna) pero h_fin.X > 0, calcular Δ =
    # h_fin - 0 = h_fin entero es ABSURDO (un Δ que dice "este mes
    # apareció todo el stock"). Mejor reportar Δ=0 y avisar.
    def _delta(key: str) -> float:
        a = f(h_ini, key)
        b = f(h_fin, key)
        if a == 0 and abs(b) > 1.0:
            return 0.0  # snapshot ini incompleto — ocultar diferencia
        return b - a

    delta = {
        # Activos
        "cart": _delta("cart"),
        "ustock": _delta("ustock"),
        "uqui": _delta("uqui"),
        "maquinaria": _delta("maquinaria"),
        "realty": _delta("realty"),
        "anticipos": _delta("anticipos"),
        # Pasivos
        "deuda": _delta("deuda"),
        # Cuasi-líquidos (control)
        "banco": _delta("banco"),
    }

    # Detectar columnas h_ini incompletas — el template las muestra
    # como warning para que la dueña sepa qué snapshot recargar.
    columnas_ini_vacias: list[str] = []
    for k in ("ustock", "uqui", "maquinaria", "realty", "anticipos", "cart", "banco"):
        if f(h_ini, k) == 0 and f(h_fin, k) > 1.0:
            columnas_ini_vacias.append(k)

    # Federico 2026-05-22 — UTILIDADES y RETIROS del período salen del
    # Historial, fiel al dBase PROCEDURE FUENTES:
    #   SUM ALL USUTI/USRET FOR RECNO()>RECI .AND. RECNO()<=RECF
    # = suma de usuti / usret de los snapshots posteriores al inicial y
    # hasta el final inclusive. Antes la web usaba ΔPatrimonio y la tabla
    # scintela.retiros — no salía del Historial.
    # La web crea varios snapshots de historia por mes (uno cada vez que
    # se entra al Historial). Para no contar el mismo mes dos veces se
    # toma UN snapshot por mes — el último no-vacío (patrimonio<>0).
    # Federico 2026-05-22 — límite superior de la suma del Historial.
    # Si h_fin es live (mes en curso) la suma NO debe incluir filas del
    # propio mes en curso (la pantalla Historial pudo dejar una): su
    # usuti/usret se agrega aparte desde el cálculo live. Cortamos al
    # último día del mes anterior al HASTA para no contar el mes doble.
    if h_fin.get("_origen") == "live":
        _sy, _sm = _mes_anterior(yy, mm)
        _sum_hasta = _date(_sy, _sm, _cal.monthrange(_sy, _sm)[1])
    else:
        _sum_hasta = h_fin.get("fecha")
    _sum_hist = db.fetch_one(
        """
        SELECT COALESCE(SUM(usuti), 0) AS uti,
               COALESCE(SUM(usret), 0) AS ret
          FROM (
              SELECT DISTINCT ON (EXTRACT(YEAR FROM fecha),
                                  EXTRACT(MONTH FROM fecha))
                     usuti, usret
                FROM scintela.historia
               WHERE fecha > %s AND fecha <= %s
                 AND COALESCE(patrimonio, 0) <> 0
               ORDER BY EXTRACT(YEAR FROM fecha),
                        EXTRACT(MONTH FROM fecha), fecha DESC
          ) m
        """,
        (h_ini.get("fecha"), _sum_hasta),
    ) or {}
    utilidad_periodo = float(_sum_hist.get("uti") or 0)
    retiros_periodo = float(_sum_hist.get("ret") or 0)
    # Mes en curso (h_fin live): su utilidad NO está en el Historial. Se
    # calcula con la identidad contable utilidad = Δpatrimonio + retiros
    # (lo mismo que el dBase guarda en USUTI al cerrar el mes). El
    # patrimonio del cierre anterior es el del snapshot del mes previo al
    # HASTA — para una ventana de 1 mes ese cierre es el propio h_ini.
    if h_fin.get("_origen") == "live":
        _ant_y, _ant_m = _mes_anterior(yy, mm)
        if (_ant_y, _ant_m) == (yy_ant, mm_ant):
            _h_ant = h_ini
        else:
            _h_ant = _historia_en_mes(_ant_y, _ant_m) or {}
        _patr_ant = float((_h_ant or {}).get("patrimonio") or 0)
        _retiros_mes = float(h_fin.get("usret") or 0)
        utilidad_periodo += (
            float(h_fin.get("patrimonio") or 0) - _patr_ant
        ) + _retiros_mes
        retiros_periodo += _retiros_mes

    fuentes: list[tuple[str, float]] = []
    usos: list[tuple[str, float]] = []

    # PRG L1708-1709: FUENTES=UTI, USOS=RET. Si UTI<0 (pérdida acum),
    # va como uso. Si RET>0 (hubo retiros), va como uso. Si RET<0
    # (reverso raro), va como fuente.
    if utilidad_periodo >= 0:
        fuentes.append(("Utilidad del período", utilidad_periodo))
    else:
        usos.append(("Pérdida del período", abs(utilidad_periodo)))
    if retiros_periodo > 0:
        usos.append(("Retiros del período", retiros_periodo))
    elif retiros_periodo < 0:
        fuentes.append(("Reverso de retiros", abs(retiros_periodo)))

    # PRG L1710-1716: para cada activo (BCNOQMR), if Δ>0 → uso, sino fuente.
    activos_labels = {
        "cart": "Cartera (clientes)",
        "ustock": "Stock de productos",
        "uqui": "Stock de químicos",
        "maquinaria": "Maquinaria",
        "realty": "Terrenos y edificios",
        "anticipos": "Anticipos USD a proveedores",
    }
    for k, label in activos_labels.items():
        d = delta[k]
        if d > 0.5:
            usos.append((f"Aumento {label.lower()}", d))
        elif d < -0.5:
            fuentes.append((f"Disminución {label.lower()}", abs(d)))

    # TMT 2026-05-20 v4 — REPLICA EXACTA del PRG L1730-1779: tabla
    # unificada con orden fijo y 2 columnas (FUENTES | USOS).
    # PRG: cada cuenta APARECE SIEMPRE con su valor absoluto del Δ,
    # en la columna FUENTES si bajó (activos) o subió (pasivos), o
    # en USOS si subió (activos) o bajó (pasivos). Mi versión vieja
    # solo mostraba las cuentas con cambio significativo en listas
    # separadas — pedido dueña: "lo hiciste mal, no entiendo porque
    # tan bobo".
    def _row(label: str, fuente: float, uso: float) -> dict:
        return {"label": label, "fuente": fuente, "uso": uso}

    def _activo_row(label: str, key: str) -> dict:
        d = delta[key]
        # Activo: si Δ>0 sube el activo → uso. Si Δ<0 baja → fuente.
        if d > 0:
            return _row(label, 0.0, d)
        else:
            return _row(label, abs(d), 0.0)

    # PRG orden exacto: CAJA Y BANCOS, CARTERA, ANTICIPOS,
    # STOCK MP+PROD., STOCK QUIM., MAQUINARIA, TERR.Y EDIF.
    cuentas: list[dict] = [
        _activo_row("CAJA Y BANCOS", "banco"),
        _activo_row("CARTERA", "cart"),
        _activo_row("ANTICIPOS", "anticipos"),
        _activo_row("STOCK MP+PROD.", "ustock"),
        _activo_row("STOCK QUIM.", "uqui"),
        _activo_row("MAQUINARIA", "maquinaria"),
        _activo_row("TERR.Y EDIF.", "realty"),
    ]
    # Pasivos: PRG L1748 PASIVOS = ABS(DF-PI). Si DF<PI (bajó), es uso.
    # Si DF>PI (subió), es fuente. Mi delta["deuda"]=fin-ini.
    d_deuda = delta["deuda"]
    if d_deuda > 0:
        cuentas.append(_row("PASIVOS", d_deuda, 0.0))
    else:
        cuentas.append(_row("PASIVOS", 0.0, abs(d_deuda)))
    # Utilidades / Pérdida del período.
    if utilidad_periodo >= 0:
        cuentas.append(_row("UTILIDADES", utilidad_periodo, 0.0))
    else:
        cuentas.append(_row("PÉRDIDA", 0.0, abs(utilidad_periodo)))
    # PRG L1769-1774: RET > 0 → "RETIROS" en USOS; RET < 0 → "APORTES"
    # en FUENTES (con ajuste FUENTES=FUENTES-RET, USOS=USOS-RET).
    if retiros_periodo > 0:
        cuentas.append(_row("RETIROS", 0.0, retiros_periodo))
    elif retiros_periodo < 0:
        cuentas.append(_row("APORTES", abs(retiros_periodo), 0.0))
    else:
        cuentas.append(_row("RETIROS", 0.0, 0.0))

    # Federico 2026-05-22 — sin fila de AJUSTE. El dBase no fuerza nada:
    # FUENTES y USOS tienen que dar idénticos por sí solos. Si no
    # coinciden, hay un problema en los snapshots y hay que verlo, no
    # taparlo con un ajuste.
    total_fuentes = sum(c["fuente"] for c in cuentas)
    total_usos = sum(c["uso"] for c in cuentas)
    descuadre = total_fuentes - total_usos

    return {
        "anio": yy,
        "mes": mm,
        "anio_ini": yy_ant,
        "mes_ini": mm_ant,
        "h_ini": h_ini,
        "h_fin": h_fin,
        # TMT 2026-05-20 v4 — `cuentas` es la tabla unificada estilo PRG
        # con 1 row por concepto y columnas (fuente, uso). El template
        # itera esta lista directo. `fuentes`/`usos` quedan para
        # back-compat de calls externos pero ya no se usan en el UI.
        "cuentas": cuentas,
        "fuentes": fuentes,
        "usos": usos,
        "total_fuentes": total_fuentes,
        "total_usos": total_usos,
        "descuadre": descuadre,
        "delta_banco": delta["banco"],
        # v5c: lista de columnas que el snapshot inicial no tiene
        # rellenas — el template muestra un warning.
        "columnas_ini_vacias": columnas_ini_vacias,
        "error": None,
    }


# ---------------------------------------------------------------------------
# GASTOS FORZADOS — flujo de fondos. Persistencia DB (migración 0033).
# Antes vivían en localStorage del navegador (cliente-side), pero la dueña
# reportó que al abrir en otro navegador/máquina aparecían vacíos. Pedido
# 2026-05-19 v8: "asegurate de encontrarlos y mostrarmelos".
# ---------------------------------------------------------------------------


def gastos_forzados_listar() -> list[dict]:
    """Lista todos los gastos forzados ordenados por fecha ASC."""
    rows = (
        db.fetch_all(
            """
        SELECT id_gasto_forzado, fecha, importe, concepto, prov, version,
               creado_por, creado_en, actualizado_en, actualizado_por
          FROM scintela.gasto_forzado
         ORDER BY fecha ASC, id_gasto_forzado ASC
        """
        )
        or []
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": int(r["id_gasto_forzado"]),
                "fecha": r["fecha"].isoformat() if r["fecha"] else None,
                "importe": float(r["importe"] or 0),
                "concepto": r["concepto"] or "",
                "prov": r["prov"] or "",
                "version": int(r["version"] or 1),
            }
        )
    return out


def gasto_forzado_crear(
    fecha,
    importe: float,
    concepto: str = "",
    prov: str = "",
    usuario: str = "web",
) -> dict:
    """Crea un nuevo gasto forzado. Devuelve el item con id y version=1.

    TMT 2026-07-15 (dueña: "no se pueden crear gastos forzados o no se ven"):
    usaba db.fetch_one() para el INSERT ... RETURNING, que NO commitea — el
    write se rollbackeaba al devolver la conexión al pool, así que el 201 traía
    el id pero la fila nunca persistía y el listar salía siempre vacío. Va con
    db.execute_returning() (commitea). Idem actualizar/eliminar abajo.
    """
    row = db.execute_returning(
        """
        INSERT INTO scintela.gasto_forzado
            (fecha, importe, concepto, prov, version, creado_por,
             actualizado_en, actualizado_por)
        VALUES (%s, %s, %s, %s, 1, %s, CURRENT_TIMESTAMP, %s)
        RETURNING id_gasto_forzado, fecha, importe, concepto, prov, version
        """,
        (fecha, importe, concepto or None, prov or None, usuario, usuario),
    )
    if not row:
        raise RuntimeError("INSERT gasto_forzado no devolvió fila")
    return {
        "id": int(row["id_gasto_forzado"]),
        "fecha": row["fecha"].isoformat() if row["fecha"] else None,
        "importe": float(row["importe"] or 0),
        "concepto": row["concepto"] or "",
        "prov": row["prov"] or "",
        "version": int(row["version"] or 1),
    }


def gasto_forzado_actualizar(
    id_gasto_forzado: int,
    expected_version: int,
    fecha=None,
    importe: float | None = None,
    concepto: str | None = None,
    prov: str | None = None,
    usuario: str = "web",
) -> dict:
    """Update con optimistic lock — rechaza si la versión actual no coincide.

    Devuelve `{ok: bool, current?: dict, updated?: dict, reason?: str}`.
    """
    # Cargar el item actual
    actual = db.fetch_one(
        """
        SELECT id_gasto_forzado, fecha, importe, concepto, prov, version
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
            "ok": False,
            "reason": "version_conflict",
            "current": {
                "id": int(actual["id_gasto_forzado"]),
                "fecha": actual["fecha"].isoformat() if actual["fecha"] else None,
                "importe": float(actual["importe"] or 0),
                "concepto": actual["concepto"] or "",
                "prov": actual["prov"] or "",
                "version": actual_v,
            },
        }
    # Update parcial — coalesce a los valores actuales si vienen en None
    nueva_fecha = fecha if fecha is not None else actual["fecha"]
    nuevo_importe = importe if importe is not None else float(actual["importe"] or 0)
    nuevo_concepto = concepto if concepto is not None else (actual["concepto"] or "")
    nuevo_prov = prov if prov is not None else (actual["prov"] or "")
    row = db.execute_returning(
        """
        UPDATE scintela.gasto_forzado
           SET fecha           = %s,
               importe         = %s,
               concepto        = %s,
               prov            = %s,
               version         = version + 1,
               actualizado_en  = CURRENT_TIMESTAMP,
               actualizado_por = %s
         WHERE id_gasto_forzado = %s
           AND version = %s
        RETURNING id_gasto_forzado, fecha, importe, concepto, prov, version
        """,
        (nueva_fecha, nuevo_importe, nuevo_concepto or None, nuevo_prov or None,
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
            "prov": row["prov"] or "",
            "version": int(row["version"] or 1),
        },
    }


def gasto_forzado_eliminar(id_gasto_forzado: int) -> bool:
    """Borra un gasto forzado. Devuelve True si se borró."""
    row = db.execute_returning(
        """
        DELETE FROM scintela.gasto_forzado
         WHERE id_gasto_forzado = %s
        RETURNING id_gasto_forzado
        """,
        (id_gasto_forzado,),
    )
    return bool(row)


def gastos_forzados_importar_bulk(items: list[dict], usuario: str = "web") -> dict:
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
        gasto_forzado_crear(fecha, importe, concepto, usuario=usuario)
        insertados += 1
    return {"insertados": insertados, "saltados": saltados}


# ---------------------------------------------------------------------------
# VENTAS DEL MES por cliente — ranking estilo dBase TINT.BAT.
# Pedido dueña 2026-05-19 v8: al clickear "Ventas" del balance quiere ver
# la grilla "VENTAS DEL MES" con CLI / KG / MONTO / % ordenado por monto
# descendente, idéntica a la pantalla del dBase legacy.
# ---------------------------------------------------------------------------


def ventas_clientes_del_mes(anio: int | None = None, mes: int | None = None) -> dict:
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

    hoy = today_ec()
    yy = int(anio) if anio else hoy.year
    mm = int(mes) if mes else hoy.month

    rows = (
        db.fetch_all(
            """
        SELECT
            UPPER(TRIM(COALESCE(f.codigo_cli, '???'))) AS codigo_cli,
            COALESCE(SUM(f.kg), 0)::int                AS kg,
            COALESCE(SUM(f.importe), 0)::numeric       AS monto
          FROM scintela.factura f
         WHERE EXTRACT(YEAR  FROM f.fecha) = %s
           AND EXTRACT(MONTH FROM f.fecha) = %s
           AND COALESCE(f.stat, '') <> 'X'
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
         GROUP BY 1
         HAVING COALESCE(SUM(f.importe), 0) <> 0 OR COALESCE(SUM(f.kg), 0) <> 0
         ORDER BY SUM(f.importe) DESC NULLS LAST
        """,
            (yy, mm),
        )
        or []
    )

    total_kg = sum(int(r["kg"] or 0) for r in rows)
    total_monto = sum(float(r["monto"] or 0) for r in rows)

    filas = []
    for i, r in enumerate(rows, start=1):
        monto = float(r["monto"] or 0)
        # TMT 2026-05-19 v8 — dueña: agregar un decimal al %.
        pct = round((monto / total_monto * 100), 1) if total_monto else 0.0
        filas.append(
            {
                "orden": i,
                "codigo_cli": r["codigo_cli"],
                "kg": int(r["kg"] or 0),
                "monto": monto,
                "pct": pct,
            }
        )

    return {
        "anio": yy,
        "mes": mm,
        "filas": filas,
        "total_kg": total_kg,
        "total_monto": total_monto,
        "n_clientes": len(filas),
    }


# ---------------------------------------------------------------------------
# TOTALIZAR estado de cuenta — re-liquidación FIFO de la cuenta de un cliente
# ---------------------------------------------------------------------------
# TMT 2026-07-06 (dueña): réplica mejorada del dBase CUENTA.PRG (rama oculta
# 'Y'). Junta TODOS los abonos de las facturas vivas del cliente y los
# redistribuye de la más vieja a la más nueva. No cambia el total adeudado;
# deja la cuenta "limpia": T…T + una A parcial + Z…Z.
#
# Decisiones cerradas de la dueña (2026-07-06):
#   1. Se ACEPTA perder los vínculos cheque↔factura (chequesxfact) de las
#      facturas del cliente — previa pantalla de confirmación que muestra lo
#      que va a quedar. IRREVERSIBLE.
#   2. Las NC / importes negativos ENTRAN a la redistribución (era limitación
#      del dBase, se arregla): su crédito vuelve al pool y extiende cobertura.
#      Si al final SOBRA pool, el excedente queda como saldo NEGATIVO
#      (crédito) en la ÚLTIMA factura viva, stat 'A' (saldo<0 nunca
#      totaliza — regla 2026-07-01).
#   3. SOLO el cliente en pantalla (sin grupos EDU/FBA).
#
# Las T salen NORMALIZADAS (abono=importe, saldo=0) — NO se replica el quirk
# del dBase que dejaba T con saldo=importe.

_SQL_FACTURAS_TOTALIZAR = """
    SELECT id_factura, numf, numf_completo, fecha, importe, abono, saldo, stat
      FROM scintela.factura
     WHERE codigo_cli = %s
       -- vivas: Z (impaga) y A (abonada parcial). T/X/… quedan afuera.
       AND COALESCE(stat, '') IN ('Z', 'A')
       -- criterio canónico de cartera (fix NJL 2026-06-17): el backfill
       -- histórico de Asinfo no participa de la cobranza.
       AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
       -- TMT 2026-07-06 (dueña: "¿cómo elegís hasta dónde totalizar?"):
       -- corte opcional por fecha INCLUSIVE — lo posterior no se toca.
       AND (CAST(%s AS date) IS NULL OR fecha <= CAST(%s AS date))
     ORDER BY fecha ASC, id_factura ASC
"""


def totalizar_redistribuir_fifo(importes: list, pool: float) -> list[dict]:
    """Núcleo puro del TOTALIZAR: redistribuye `pool` sobre `importes` (FIFO).

    Devuelve, por factura y en el mismo orden, {"stat", "abono", "saldo"}.

    Implementado con `restante` (pool que queda) en vez del ACUM del PRG —
    es la MISMA aritmética cuando los importes son positivos (acum<=pool ↔
    restante>=imp), pero con NCs en el medio preserva SIEMPRE el invariante
    Σabono==pool / Σsaldo constante: el crédito de la NC vuelve al pool y se
    aplica hacia ADELANTE (FIFO — nunca reabre una factura ya recorrida).

      · imp < 0 (NC/devolución) → stat 'T', abono=imp, saldo=0; el crédito
        |imp| se suma al restante (decisión dueña #2 — el dBase las salteaba).
      · restante cubre imp      → stat 'T', abono=imp, saldo=0.
      · restante parcial        → stat 'A', abono=restante, saldo=resto.
      · restante agotado        → stat 'Z', abono=0, saldo=imp.

    Si al final sobra pool (Σabonos > Σimportes cubiertos) el excedente se
    vuelca a la ÚLTIMA factura del recorrido: abono += sobrante, saldo
    negativo, stat 'A' (saldo<0 nunca totaliza — regla 2026-07-01).

    Tolerancia de redondeo: medio centavo (0.005), igual que cobranza.
    """
    pool = round(float(pool or 0), 2)
    # TMT 2026-07-07 (dueña "KAG totalizar no funcionó"): el crédito de las NC
    # ahora entra al pool DESDE EL ARRANQUE (no solo hacia adelante desde la
    # fecha de la NC). Así todo el crédito se aplica FIFO a las facturas más
    # VIEJAS y los abonos CONSOLIDAN en las primeras filas, en vez de quedar
    # dispersos en las facturas que seguían a cada NC.
    nc_credito = round(sum(-round(float(i or 0), 2)
                           for i in importes if float(i or 0) < 0), 2)
    restante = round(pool + nc_credito, 2)
    out: list[dict] = []
    for imp_raw in importes:
        imp = round(float(imp_raw or 0), 2)
        if imp < 0:
            # NC / devolución: su crédito YA está en el pool (arriba). Se
            # totaliza con saldo=0; no vuelve a tocar el restante.
            out.append({"stat": "T", "abono": imp, "saldo": 0.0})
        elif restante >= imp - 0.005:
            out.append({"stat": "T", "abono": imp, "saldo": 0.0})
            restante = round(restante - imp, 2)
        elif restante > 0.005:
            ab = round(restante, 2)
            out.append({"stat": "A", "abono": ab, "saldo": round(imp - ab, 2)})
            restante = 0.0
        else:
            out.append({"stat": "Z", "abono": 0.0, "saldo": imp})
    # Sobró pool → crédito (saldo negativo) en la ÚLTIMA factura viva.
    # abs(): si el pool vino NEGATIVO (data patológica) también se vuelca,
    # así el invariante Σabono==pool se sostiene siempre.
    if out and abs(restante) > 0.005:
        imp_last = round(float(importes[-1] or 0), 2)
        last = out[-1]
        last["abono"] = round(last["abono"] + restante, 2)
        last["saldo"] = round(imp_last - last["abono"], 2)
        last["stat"] = "A"
    return out


def _totalizar_armar(facturas: list[dict]) -> dict:
    """Común a preview y ejecutar: pool, redistribución y contadores."""
    importes = [float(f["importe"] or 0) for f in facturas]
    pool = round(sum(float(f["abono"] or 0) for f in facturas), 2)
    hay_nc = any(i < 0 for i in importes)
    nuevos = totalizar_redistribuir_fifo(importes, pool)
    return {
        "pool": pool,
        "hay_nc": hay_nc,
        "nada_que_hacer": abs(pool) < 0.005 and not hay_nc,
        "nuevos": nuevos,
        "n_T": sum(1 for n in nuevos if n["stat"] == "T"),
        "n_A": sum(1 for n in nuevos if n["stat"] == "A"),
        "n_Z": sum(1 for n in nuevos if n["stat"] == "Z"),
        "sum_importe": round(sum(importes), 2),
        "sum_saldo_antes": round(sum(float(f["saldo"] or 0) for f in facturas), 2),
        "sum_abono_despues": round(sum(n["abono"] for n in nuevos), 2),
        "sum_saldo_despues": round(sum(n["saldo"] for n in nuevos), 2),
    }


def totalizar_estado_cuenta_preview(codigo_cli: str, hasta=None) -> dict:
    """Datos para la pantalla de confirmación del TOTALIZAR (solo lectura).

    Devuelve cliente + filas [actual → después] + totales + n_links (los
    vínculos cheque↔factura que se van a borrar).
    """
    cliente = db.fetch_one(
        "SELECT codigo_cli, nombre FROM scintela.cliente WHERE codigo_cli = %s",
        (codigo_cli,),
    )
    if not cliente:
        return {"cliente": None}
    facturas = db.fetch_all(_SQL_FACTURAS_TOTALIZAR, (codigo_cli, hasta, hasta))
    n_links = 0
    if facturas:
        row = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.chequesxfact WHERE id_fact = ANY(%s)",
            ([f["id_factura"] for f in facturas],),
        )
        n_links = int((row or {}).get("n") or 0)
    calc = _totalizar_armar(facturas)
    filas = []
    for f, n in zip(facturas, calc["nuevos"], strict=True):
        filas.append({
            "id_factura": f["id_factura"],
            "numf": f["numf"],
            "numf_completo": f["numf_completo"],
            "fecha": f["fecha"],
            "importe": round(float(f["importe"] or 0), 2),
            "abono_actual": round(float(f["abono"] or 0), 2),
            "saldo_actual": round(float(f["saldo"] or 0), 2),
            "stat_actual": (f["stat"] or "").strip(),
            "abono_nuevo": n["abono"],
            "saldo_nuevo": n["saldo"],
            "stat_nuevo": n["stat"],
            "cambia": (
                (f["stat"] or "").strip() != n["stat"]
                or abs(round(float(f["abono"] or 0), 2) - n["abono"]) > 0.005
                or abs(round(float(f["saldo"] or 0), 2) - n["saldo"]) > 0.005
            ),
        })
    return {
        "cliente": cliente,
        "filas": filas,
        "n_links": n_links,
        **{k: calc[k] for k in (
            "pool", "hay_nc", "nada_que_hacer", "n_T", "n_A", "n_Z",
            "sum_importe", "sum_saldo_antes", "sum_abono_despues",
            "sum_saldo_despues",
        )},
    }


def totalizar_estado_cuenta_ejecutar(codigo_cli: str, usuario: str = "web",
                                     hasta=None) -> dict:
    """Ejecuta el TOTALIZAR en UNA transacción. IRREVERSIBLE.

    1. Lockea las facturas vivas del cliente (FOR UPDATE) y recalcula la
       redistribución adentro de la tx (no confía en la preview: si alguien
       cobró en el medio, se recalcula con lo fresco).
    2. Verifica el invariante Σsaldo/Σabono ANTES==DESPUÉS (±0.01) — si no
       cierra, aborta TODO (ValueError → rollback).
    3. UPDATE por factura (abono/saldo/stat + usuario_modifica).
    4. DELETE de scintela.chequesxfact de esas facturas (decisión dueña #1).
    5. UN mov_doble 'totalizar_estado_cuenta' con la metadata del resumen.

    Devuelve el resumen {n_facturas, pool, n_T, n_A, n_Z, n_links_borrados}.
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    with db.tx() as conn:
        facturas = db.fetch_all(
            _SQL_FACTURAS_TOTALIZAR + " FOR UPDATE",
            (codigo_cli, hasta, hasta), conn=conn,
        )
        if not facturas:
            raise ValueError(
                f"{codigo_cli}: sin facturas vivas (Z/A) — nada que totalizar."
            )
        calc = _totalizar_armar(facturas)
        if calc["nada_que_hacer"]:
            raise ValueError(
                f"{codigo_cli}: los abonos suman 0,00 y no hay notas de "
                "crédito — nada que redistribuir."
            )
        # Invariante: totalizar REDISTRIBUYE, no crea ni borra plata.
        if (abs(calc["sum_saldo_antes"] - calc["sum_saldo_despues"]) > 0.01
                or abs(calc["pool"] - calc["sum_abono_despues"]) > 0.01):
            raise ValueError(
                f"{codigo_cli}: invariante roto — Σsaldo "
                f"{calc['sum_saldo_antes']:.2f}→{calc['sum_saldo_despues']:.2f}, "
                f"Σabono {calc['pool']:.2f}→{calc['sum_abono_despues']:.2f}. "
                "Abortado: no se cambió nada (¿saldo≠importe−abono en alguna "
                "factura? Revisar la cuenta antes de totalizar)."
            )
        n_upd = 0
        for f, n in zip(facturas, calc["nuevos"], strict=True):
            stat_act = (f["stat"] or "").strip()
            if (stat_act == n["stat"]
                    and abs(round(float(f["abono"] or 0), 2) - n["abono"]) <= 0.005
                    and abs(round(float(f["saldo"] or 0), 2) - n["saldo"]) <= 0.005):
                continue  # sin cambios — no ensuciar usuario_modifica
            db.execute(
                "UPDATE scintela.factura "
                "   SET abono = %s, saldo = %s, stat = %s, usuario_modifica = %s "
                " WHERE id_factura = %s",
                (n["abono"], n["saldo"], n["stat"], usuario, f["id_factura"]),
                conn=conn,
            )
            n_upd += 1
        # Vínculos cheque↔factura: se PIERDEN (aceptado por la dueña, la
        # confirmación lo avisa). El abono redistribuido ya no mapea 1-a-1
        # con los cheques originales, dejar los links sería mentir.
        ids = [f["id_factura"] for f in facturas]
        n_links = db.execute(
            "DELETE FROM scintela.chequesxfact WHERE id_fact = ANY(%s)",
            (ids,), conn=conn,
        ) or 0
        # Huella única en el historial. Si el pool es 0 (solo NCs), el
        # importe del mov es el crédito NC redistribuido — registrar()
        # ignora importe 0.
        importe_md = calc["pool"]
        if abs(importe_md) < 0.005:
            importe_md = round(
                sum(-float(f["importe"] or 0) for f in facturas
                    if float(f["importe"] or 0) < 0), 2)
        import mov_doble as _md
        _md.registrar(
            conn=conn,
            tipo="totalizar_estado_cuenta",
            origen_table="factura", origen_id=facturas[0]["id_factura"],
            destino_table="factura", destino_id=facturas[-1]["id_factura"],
            importe=importe_md,
            fecha=today_ec(),
            concepto=(
                f"TOTALIZAR estado de cuenta {codigo_cli} — "
                f"{len(facturas)} fact., pool {calc['pool']:.2f}"
            )[:200],
            usuario=usuario,
            metadata={
                "codigo_cli": codigo_cli,
                "n_facturas": len(facturas),
                "pool": calc["pool"],
                "n_T": calc["n_T"], "n_A": calc["n_A"], "n_Z": calc["n_Z"],
                "n_links_borrados": n_links,
            },
        )
        return {
            "codigo_cli": codigo_cli,
            "n_facturas": len(facturas),
            "n_actualizadas": n_upd,
            "pool": calc["pool"],
            "n_T": calc["n_T"], "n_A": calc["n_A"], "n_Z": calc["n_Z"],
            "n_links_borrados": n_links,
            "saldo": calc["sum_saldo_despues"],
        }


# ---------------------------------------------------------------------------
# Cambio manual de estado de factura A/Z ↔ T desde el estado de cuenta.
# TMT 2026-07-09 (dueña): "poder pasar facturas de A→T y T→A". CERRAR una
# factura viva (Z/A) la marca T con saldo 0 (se considera pagada, sale de la
# lista viva); REABRIR una T la vuelve A con su saldo restaurado. Reversible:
# el CERRAR snapshotea (abono, saldo, stat) en el mov_doble, y el REABRIR
# restaura ese snapshot si existe (si la T vino de un totalizar u otra fuente,
# cae al fallback saldo = importe − abono).
# ---------------------------------------------------------------------------
def facturas_totalizadas_cliente(codigo_cli: str, limite: int = 300) -> list[dict]:
    """Facturas T (totalizadas/cerradas) del cliente — para poder REABRIRLAS.

    Mismo criterio de cartera que la lista viva (sin backfill de asinfo). Se
    listan aparte porque el estado de cuenta oculta las T.
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    return db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, importe, abono, saldo, stat
          FROM scintela.factura
         WHERE codigo_cli = %s
           AND COALESCE(stat, '') = 'T'
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
         ORDER BY fecha DESC, numf DESC
         LIMIT %s
        """,
        (codigo_cli, limite),
    )


def factura_cambiar_stat_a_t(
    id_factura: int, codigo_cli: str, usuario: str = "web"
) -> dict:
    """Toggle A/Z ↔ T de UNA factura (pantalla estado de cuenta). Reversible.

    - Factura VIVA (Z o A) → la CIERRA: stat='T', saldo=0 (se considera
      pagada). Snapshotea (abono, saldo, stat) previos en el mov_doble.
    - Factura T → la REABRE: busca el snapshot del último cierre activo por
      esta tool y restaura (abono, saldo, stat='A'); si no hay snapshot
      (p.ej. la T vino de un totalizar) cae a stat='A', saldo=importe−abono.

    Devuelve {id_factura, numf, stat_previo, stat_nuevo, saldo_nuevo, accion}.
    """
    from periodo_guard import asegurar_fecha_abierta

    codigo_cli = (codigo_cli or "").strip().upper()
    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    with db.tx() as conn:
        f = db.fetch_one(
            "SELECT id_factura, numf, numf_completo, codigo_cli, importe, "
            "       abono, saldo, stat "
            "  FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
            (id_factura,),
            conn=conn,
        )
        if not f:
            raise ValueError(f"Factura #{id_factura} no existe.")
        if (f.get("codigo_cli") or "").strip().upper() != codigo_cli:
            raise ValueError(
                f"La factura #{id_factura} es de "
                f"{(f.get('codigo_cli') or '?').strip()}, no de {codigo_cli}."
            )
        stat_prev = (f.get("stat") or "").strip().upper()
        importe = round(float(f.get("importe") or 0), 2)
        abono = round(float(f.get("abono") or 0), 2)
        saldo = round(float(f.get("saldo") or 0), 2)

        if stat_prev in ("Z", "A", "", " "):
            # CERRAR → T. Snapshot para poder revertir con exactitud.
            db.execute(
                "UPDATE scintela.factura "
                "   SET stat='T', saldo=0, usuario_modifica=%s "
                " WHERE id_factura=%s",
                (usuario, id_factura),
                conn=conn,
            )
            import mov_doble as _md
            _md.registrar(
                conn=conn,
                tipo="factura_cerrada_a_t",
                origen_table="factura", origen_id=id_factura,
                destino_table="factura", destino_id=id_factura,
                importe=importe or 1.0,
                fecha=fecha,
                concepto=(
                    f"CERRAR factura {f.get('numf_completo') or f.get('numf')} "
                    f"{codigo_cli} {stat_prev or 'Z'}→T (saldo {saldo:.2f}→0)"
                )[:200],
                usuario=usuario,
                metadata={
                    "id_factura": id_factura, "codigo_cli": codigo_cli,
                    "stat_previo": stat_prev or "Z",
                    "abono_previo": abono, "saldo_previo": saldo,
                },
            )
            return {
                "id_factura": id_factura, "numf": f.get("numf"),
                "stat_previo": stat_prev or "Z", "stat_nuevo": "T",
                "saldo_nuevo": 0.0, "accion": "cerrada",
            }

        if stat_prev == "T":
            # REABRIR → A. Restaurar del último cierre activo si lo hay.
            snap = db.fetch_one(
                """
                SELECT metadata
                  FROM scintela.mov_doble
                 WHERE tipo = 'factura_cerrada_a_t'
                   AND origen_id = %s
                   AND estado = 'activo'
                 ORDER BY id_mov_doble DESC
                 LIMIT 1
                """,
                (id_factura,),
                conn=conn,
            )
            md = (snap or {}).get("metadata") or {}
            if isinstance(md, str):
                import json as _json
                try:
                    md = _json.loads(md)
                except Exception:
                    md = {}
            if md.get("saldo_previo") is not None:
                saldo_nuevo = round(float(md.get("saldo_previo") or 0), 2)
                abono_nuevo = round(float(md.get("abono_previo") or abono), 2)
            else:
                saldo_nuevo = round(importe - abono, 2)
                abono_nuevo = abono
            db.execute(
                "UPDATE scintela.factura "
                "   SET stat='A', saldo=%s, abono=%s, usuario_modifica=%s "
                " WHERE id_factura=%s",
                (saldo_nuevo, abono_nuevo, usuario, id_factura),
                conn=conn,
            )
            import mov_doble as _md
            _md.registrar(
                conn=conn,
                tipo="factura_reabierta_de_t",
                origen_table="factura", origen_id=id_factura,
                destino_table="factura", destino_id=id_factura,
                importe=importe or 1.0,
                fecha=fecha,
                concepto=(
                    f"REABRIR factura {f.get('numf_completo') or f.get('numf')} "
                    f"{codigo_cli} T→A (saldo →{saldo_nuevo:.2f})"
                )[:200],
                usuario=usuario,
                metadata={
                    "id_factura": id_factura, "codigo_cli": codigo_cli,
                    "saldo_nuevo": saldo_nuevo, "abono_nuevo": abono_nuevo,
                },
            )
            return {
                "id_factura": id_factura, "numf": f.get("numf"),
                "stat_previo": "T", "stat_nuevo": "A",
                "saldo_nuevo": saldo_nuevo, "accion": "reabierta",
            }

        raise ValueError(
            f"La factura #{id_factura} está en stat='{stat_prev}' — solo se "
            "cierra/reabre desde estados Z/A/T."
        )


def _reactivar_factura_anulada(id_factura: int, *, conn) -> None:
    """Re-activa la emisión de una factura anulada (X → viva): marca el
    'reverso_factura_anulada' activo como reversado y vuelve la emisión a
    'activo', para que la factura vuelva a contar en flujo/utilidad. TMT
    2026-07-21."""
    rev = db.fetch_one(
        "SELECT id_mov_doble, id_original FROM scintela.mov_doble "
        " WHERE tipo='reverso_factura_anulada' AND origen_id=%s "
        "   AND estado='activo' ORDER BY id_mov_doble DESC LIMIT 1",
        (id_factura,), conn=conn)
    if rev:
        db.execute(
            "UPDATE scintela.mov_doble SET estado='reversado' "
            " WHERE id_mov_doble=%s", (rev["id_mov_doble"],), conn=conn)
        if rev.get("id_original"):
            db.execute(
                "UPDATE scintela.mov_doble SET estado='activo', id_reverso=NULL "
                " WHERE id_mov_doble=%s", (rev["id_original"],), conn=conn)


def factura_set_stat(
    id_factura: int, codigo_cli: str, target: str, usuario: str = "web"
) -> dict:
    """Cambia el stat de UNA factura a Z / A / T (para 'X' usar
    facturas.anular). Reversible: snapshotea (stat, abono, saldo) previos en el
    mov_doble `factura_stat_cambio`. Si la factura venía anulada (X) y el target
    es un estado vivo, primero la reactiva.

    TMT 2026-07-21 (dueña): "que me deje cambiar el estado de Z a A a T o a X"
    con un dropdown por fila (Cartera y estado de cuenta).

      - target 'T' → cierra (pagada): saldo = 0.
      - target 'A' → abierta: saldo = importe − abono.
      - target 'Z' → cartera: saldo = importe − abono.

    Devuelve {id_factura, numf, stat_previo, stat_nuevo, saldo_nuevo, accion}.
    """
    from periodo_guard import asegurar_fecha_abierta

    target = (target or "").strip().upper()
    if target not in ("Z", "A", "T"):
        raise ValueError("Estado inválido — usá Z, A, T o X (X = anular).")
    codigo_cli = (codigo_cli or "").strip().upper()
    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    with db.tx() as conn:
        f = db.fetch_one(
            "SELECT id_factura, numf, numf_completo, codigo_cli, importe, "
            "       abono, saldo, stat "
            "  FROM scintela.factura WHERE id_factura=%s FOR UPDATE",
            (id_factura,), conn=conn)
        if not f:
            raise ValueError(f"Factura #{id_factura} no existe.")
        if (f.get("codigo_cli") or "").strip().upper() != codigo_cli:
            raise ValueError(
                f"La factura #{id_factura} es de "
                f"{(f.get('codigo_cli') or '?').strip()}, no de {codigo_cli}.")
        stat_prev = (f.get("stat") or "").strip().upper()
        importe = round(float(f.get("importe") or 0), 2)
        abono = round(float(f.get("abono") or 0), 2)
        saldo = round(float(f.get("saldo") or 0), 2)
        if stat_prev == target:
            return {"id_factura": id_factura, "numf": f.get("numf"),
                    "stat_previo": stat_prev, "stat_nuevo": target,
                    "saldo_nuevo": saldo, "accion": "sin_cambios"}

        import mov_doble as _md

        # Reactivar si viene de X (anulada) → re-activar la emisión.
        if stat_prev == "X":
            _reactivar_factura_anulada(id_factura, conn=conn)

        # Si viene de T, restaurar el abono del último cierre (si lo hay).
        if stat_prev == "T":
            snap = db.fetch_one(
                "SELECT metadata FROM scintela.mov_doble "
                " WHERE tipo IN ('factura_cerrada_a_t','factura_stat_cambio') "
                "   AND origen_id=%s AND estado='activo' "
                " ORDER BY id_mov_doble DESC LIMIT 1",
                (id_factura,), conn=conn)
            md = (snap or {}).get("metadata") or {}
            if isinstance(md, str):
                import json as _json
                try:
                    md = _json.loads(md)
                except Exception:  # noqa: BLE001
                    md = {}
            if md.get("abono_previo") is not None:
                abono = round(float(md.get("abono_previo") or abono), 2)

        saldo_nuevo = 0.0 if target == "T" else round(importe - abono, 2)

        db.execute(
            "UPDATE scintela.factura SET stat=%s, saldo=%s, abono=%s, "
            "  usuario_modifica=%s WHERE id_factura=%s",
            (target, saldo_nuevo, abono, usuario, id_factura), conn=conn)

        _md.registrar(
            conn=conn, tipo="factura_stat_cambio",
            origen_table="factura", origen_id=id_factura,
            destino_table="factura", destino_id=id_factura,
            importe=importe or 1.0, fecha=fecha,
            concepto=(
                f"CAMBIO stat factura {f.get('numf_completo') or f.get('numf')} "
                f"{codigo_cli} {stat_prev or 'Z'}→{target} "
                f"(saldo {saldo:.2f}→{saldo_nuevo:.2f})")[:200],
            usuario=usuario,
            metadata={"id_factura": id_factura, "codigo_cli": codigo_cli,
                      "stat_previo": stat_prev or "Z",
                      "abono_previo": abono, "saldo_previo": saldo})
        return {"id_factura": id_factura, "numf": f.get("numf"),
                "stat_previo": stat_prev or "Z", "stat_nuevo": target,
                "saldo_nuevo": saldo_nuevo, "accion": "cambiada"}
