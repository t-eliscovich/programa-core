"""Match producción tejeduría (Asinfo) ↔ compras tipo K (Programa Core).

Reemplaza la carga MANUAL del dBase. Lado Asinfo:
`modules.asinfo.service.produccion_tejeduria_mes` (orden_fabricacion bodega 52).
Lado Programa Core: `scintela.compra` tipo K (lo que hoy se carga a mano).

Match:
  · FINO (de acá para adelante): la compra creada desde la tab estampa el OFT
    en `concepto` ('OFT-000038848') → cada OF sabe si ya tiene su compra.
  · APROX (lo viejo, sin OFT): se concilia por TEJEDOR y MES. Las fechas NO
    calzan exacto (la carga manual va con lag y fechas redondas), así que el
    match viejo es "masomenos" por tejedor, nunca por día.

Todo fail-soft: si Asinfo cae, `disponible=False` y la tab muestra un aviso.
"""
import logging
import os
import re
import threading
import time as _time
from datetime import date, timedelta

import db
from filters import num_es
from modules.asinfo import service as asinfo_service
from modules.tejeduria_asinfo import queries as _tarifas

_LOG = logging.getLogger("programa_core.tejeduria")

_OFT_RE = re.compile(r"OFT-\d+", re.IGNORECASE)
#: El documento con el que se estampa la compra. Desde 2026-08-19 es el INGRESO
#: (IFT); las compras viejas llevan la ORDEN (OFT) y se siguen reconociendo.
_IFT_RE = re.compile(r"IFT-\d+", re.IGNORECASE)

# usuario_crea de las compras que crea la carga automática. `scripts/import_dbf.py`
# lo PRESERVA en el sync del dBase — igual que el puente formulas ('formulas-%'):
# estas compras nacen en PC (el DBF no las tiene, es justamente el punto) y si el
# sync las borrara no volverían nunca.
MARCADOR_CARGA = "asinfo-tejeduria"

# Tercerizados válidos (pedido dueña 2026-07-16): en la tab SOLO mostramos
# Reyes (RY) y Ponce (AP). Cualquier otro no-INTELA (R UNDA, GENERICA PRUEBAS,
# OFs sin código que salen como "?") se excluye — no sabemos qué son / son
# tests. Para sumar un tercerizado nuevo, agregá su código de proveedor acá.
# TMT 2026-07-30: entra **UN (Rodrigo Unda)**. Asinfo registraba su producción
# desde siempre (`"R UNDA …"`) pero no estaba mapeado, así que caía a INTELA y
# el motor nunca le creó una compra. Ver TEJEDOR_TERCERIZADO_PREFIJO en
# modules/asinfo/service.py.
TERCERIZADOS_VALIDOS = {"RY", "AP", "UN"}

# Orden FIJO de las columnas de tercerizados en el diario de ingreso a bodega.
# Fijo y no `sorted(TERCERIZADOS_VALIDOS)` para que las columnas no se muevan de
# lugar entre un mes y otro (la dueña las lee siempre en el mismo orden). Un
# tercerizado que esté en TERCERIZADOS_VALIDOS y NO acá igual aparece: se
# agrega al final. Así el próximo tejedor nuevo no vuelve a caer en silencio
# adentro de la columna INTELA — que es exactamente lo que le pasó a UN.
ORDEN_COLUMNAS_DIARIO = ("RY", "AP", "UN")

# Etiqueta de respaldo para el encabezado cuando el mes no tiene ninguna OF de
# ese tejedor (sin OFs no hay label que sacar de Asinfo, y la columna igual se
# dibuja para que la tabla tenga siempre las mismas columnas).
LABEL_TERCERIZADO = {"RY": "Reyes", "AP": "Ponce", "UN": "Unda"}


#: Desde cuándo el pasivo se arma con los INGRESOS (IFT). Antes de esta fecha
#: la pantalla sigue mostrando las OFs CERRADAS, exactamente como estaba.
#:
#: TMT 2026-08-19 (dueña, mirando el dry-run del barrido: *"junio seguro que no
#: se toca. ¿No se cargaron ya? Quizás no montos exactos — hacelo para
#: agosto"*). El barrido hacia atrás llegaba a junio y julio: junio salía en
#: cero solo (sus abiertas ya estaban cubiertas por facturas cargadas a mano),
#: pero julio proponía 11 compras por $ 17.205,24 y en julio hay 5 facturas
#: tipeadas a mano que no matchean con ninguna orden por kilos — o sea que el
#: pasivo de julio probablemente YA esté cargado, con otros montos. Cargar de
#: nuevo esos kilos era duplicar plata para arreglar una foto.
#:
#: Con el corte, julio y junio quedan como estaban y el mecanismo arranca
#: limpio en agosto, que no tiene NINGUNA compra K de tercerizados.
#: Correr el mecanismo sobre un mes viejo = mover esta fecha a mano.
INGRESOS_DESDE = (2026, 8)


def _cuentan_los_ingresos(anio: int, mes: int) -> bool:
    """¿El mes es del corte para acá? (ver INGRESOS_DESDE)"""
    try:
        return (int(anio), int(mes)) >= INGRESOS_DESDE
    except (TypeError, ValueError):
        return False


def _compras_k_por_prov(anio: int, mes: int) -> dict:
    """{codigo_prov: {kg, importe, n}} de scintela.compra tipo K del mes
    (kg>0, no anuladas)."""
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(COALESCE(codigo_prov, ''))) AS cod,
                   COALESCE(SUM(kg), 0)      AS kg,
                   COALESCE(SUM(importe), 0) AS importe,
                   COUNT(*)                  AS n
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(kg, 0) > 0
               AND COALESCE(stat, '') <> 'Y'
               AND EXTRACT(YEAR  FROM fecha) = %s
               AND EXTRACT(MONTH FROM fecha) = %s
             GROUP BY UPPER(TRIM(COALESCE(codigo_prov, '')))
            """,
            (int(anio), int(mes)),
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {
        r["cod"]: {
            "kg": float(r["kg"] or 0),
            "importe": float(r["importe"] or 0),
            "n": int(r["n"] or 0),
        }
        for r in rows
    }


# Ventana del tope ACUMULADO por proveedor (TMT 2026-07-26, dueña: "sí, por
# proveedor"). El mes solo no sirve: el maquilero factura con desfase y a
# caballo de dos meses (en julio el dBase le facturó a Ponce 11.415,95 kg
# contra 8.817,00 que Asinfo cerró). 3 meses = el mes que se ve + los 2
# anteriores; de sobra para un desfase que en los datos reales es de días.
MESES_VENTANA_TOPE = 3


def _rango_ventana(anio: int, mes: int, meses: int = MESES_VENTANA_TOPE):
    """(desde, hasta) ISO de la ventana que termina al final de (anio, mes)."""
    a, m = int(anio), int(mes)
    total = a * 12 + (m - 1) - (max(1, int(meses)) - 1)
    d_a, d_m = divmod(total, 12)
    desde = date(d_a, d_m + 1, 1)
    h_a, h_m = (a + 1, 1) if m == 12 else (a, m + 1)
    return desde.isoformat(), date(h_a, h_m, 1).isoformat()


def _compras_k_por_prov_rango(desde: str, hasta: str) -> dict:
    """{codigo_prov: kg} de compras tipo K en [desde, hasta) — el lado PC de
    `falta_acumulada`, que hoy es sólo DATO informativo del plan (el tope se
    sacó el 30/07). Mismo universo que `_compras_k_por_prov` (kg>0, no anuladas)."""
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(COALESCE(codigo_prov, ''))) AS cod,
                   COALESCE(SUM(kg), 0) AS kg
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(kg, 0) > 0
               AND COALESCE(stat, '') <> 'Y'
               AND fecha >= %s AND fecha < %s
             GROUP BY UPPER(TRIM(COALESCE(codigo_prov, '')))
            """,
            (desde, hasta),
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {r["cod"]: float(r["kg"] or 0) for r in rows}


def falta_acumulada(anio: int, mes: int, meses: int = MESES_VENTANA_TOPE) -> dict:
    """{cod_prov: kg que faltan cargar} ACUMULADO en la ventana — el TOPE.

    producido (Asinfo, OFs cerradas) − cargado (compras K de PC), por proveedor
    tercerizado. Fail-soft: si Asinfo no responde devuelve {} y el llamador
    NO carga nada (mejor no cargar que cargar de más).
    """
    desde, hasta = _rango_ventana(anio, mes, meses)
    prod = asinfo_service.produccion_tejeduria_rango(desde, hasta)
    if not prod.get("disponible"):
        return {}
    cargado = _compras_k_por_prov_rango(desde, hasta)
    out: dict = {}
    for t in prod.get("por_tejedor", []):
        cod = (t.get("cod") or "").upper().strip()
        if not cod or t.get("es_intela") or cod not in TERCERIZADOS_VALIDOS:
            continue
        out[cod] = round(float(t.get("kg") or 0) - cargado.get(cod, 0.0), 2)
    return out


def _ofts_estampadas() -> dict:
    """{OFT: {"importe": $, "kg": kg, "n": compras}} — lo YA CARGADO por OFT.

    Suma TODAS las compras tipo K vivas que estampan ese OFT en el concepto.
    Si una compra estampa varios OFT, se reparte en partes iguales (en la
    práctica la tab crea 1 compra por OFT, así que va entera).

    ⭐ TMT 2026-08-19: antes devolvía sólo el importe y se usaba como un sí/no
    ("esta OF ya tiene compra, no la ofrezcas más"). Con las OFs ABIERTAS eso
    ya no alcanza: el maquilero factura mientras la orden sigue abierta y los
    kilos SIGUEN SUBIENDO, así que hay que saber cuántos kg de esa OF están
    pagados para poder cargar sólo el SALDO. Por eso ahora también viaja el kg.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT concepto, COALESCE(importe, 0) AS importe,
                   COALESCE(kg, 0) AS kg
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(stat, '') <> 'Y'
               AND concepto ILIKE '%%OFT-%%'
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    out: dict = {}
    for r in rows:
        concepto = r.get("concepto") or ""
        # La compra nueva estampa "IFT-… OFT-… <producto>": el documento que
        # IDENTIFICA el ingreso es el IFT, el OFT viaja sólo para poder rastrear
        # la orden. Si se repartiera entre los dos, cada uno se llevaría la
        # mitad de los kg y el ingreso quedaría eternamente a medio cargar.
        ofts = [m.upper() for m in _IFT_RE.findall(concepto)]
        if not ofts:
            ofts = [m.upper() for m in _OFT_RE.findall(concepto)]
        if not ofts:
            continue
        parte_imp = float(r.get("importe") or 0) / len(ofts)
        parte_kg = float(r.get("kg") or 0) / len(ofts)
        for k in ofts:
            acc = out.setdefault(k, {"importe": 0.0, "kg": 0.0, "n": 0})
            acc["importe"] = round(acc["importe"] + parte_imp, 2)
            acc["kg"] = round(acc["kg"] + parte_kg, 2)
            acc["n"] += 1
    return out


def _sumar_estampada(estampadas: dict, oft: str, importe, kg) -> None:
    """Acumula en el mapa de `_ofts_estampadas` lo que este lote acaba de crear.

    Sin esto, dos OFs del mismo lote (o dos corridas seguidas del hilo de
    fondo) volverían a ver saldo entero y cargarían dos veces.
    """
    acc = estampadas.setdefault(oft, {"importe": 0.0, "kg": 0.0, "n": 0})
    acc["importe"] = round(acc["importe"] + float(importe or 0), 2)
    acc["kg"] = round(acc["kg"] + float(kg or 0), 2)
    acc["n"] += 1


def _key(of: dict) -> str:
    """Clave estable del tejedor (codigo_prov, o '?'+label si es desconocido)."""
    return of["cod"] or ("?" + of["label"])


def resumen_mes(anio: int, mes: int) -> dict:
    """Resumen de la tab: producción Asinfo + match contra compras K.

    Devuelve:
        disponible, anio, mes, total_kg,
        tejedores: [{key, cod, label, es_intela, ofs, kg,
                     compra_kg, compra_importe, compra_n, falta_kg}]  (columnas + match)
        por_dia:   [{dia, kg:{key: kg}, total}]  (resumen diario, más nuevo arriba)
        pendientes:[of...] tercerizadas SIN OFT estampado (cargables)
    """
    prod = asinfo_service.produccion_tejeduria_mes(anio, mes)
    disponible = bool(prod.get("disponible"))
    ofs = list(prod.get("ofs", []))

    # ⭐ TMT 2026-08-19 (dueña: *"nos facturan con la factura abierta, así que
    # apenas entren kg hay que pagar; esos kg tienen que figurar entrados en
    # tejeduría y aparecer el pasivo"*; y Andrés: *"existe una IFT, ingreso de
    # orden de fabricación, y ese es un mejor documento"*).
    #
    # `produccion_tejeduria_mes` sólo trae OFs CERRADAS, y en Asinfo el cierre
    # llega con 11-27 días de retraso: al 19/08 la última OF tercerizada
    # cerrada era del 28/07 y la pantalla mostraba tercerizado CERO en agosto,
    # mientras Reyes, Ponce y Unda ya habían entregado. Los kilos entraban a
    # bodega igual (el ingreso se mide por saldo, no por la OF), así que se
    # escondían dentro del residuo INTELA y nadie generaba el pasivo.
    #
    # La unidad correcta es el INGRESO (IFT), no la orden: tiene fecha propia y
    # kilos propios. Mirando la orden, todo lo que un maquilero entrega este
    # mes contra una orden abierta el mes pasado se imputaba al mes pasado —
    # Ponce entregó 8 veces en agosto, TODAS contra órdenes de julio, y quedaba
    # en CERO. Ver `asinfo_service.ingresos_fabricacion_mes`.
    ingresos = (asinfo_service.ingresos_fabricacion_mes(anio, mes)
                if (disponible and _cuentan_los_ingresos(anio, mes)) else {})
    if ingresos.get("disponible"):
        # Los tercerizados pasan a salir de los IFT. Se SACAN de la lista de
        # OFs cerradas para no contarlos dos veces: el IFT ya cubre todo lo que
        # entró, esté la orden cerrada o no. INTELA sigue viniendo de las OFs
        # (no factura: su kg es el plug contra el ingreso a bodega).
        ofs = [o for o in ofs if o.get("es_intela")]
        ofs += [o for o in (ingresos.get("ofs") or [])
                if not o.get("es_intela")]

    # TMT 2026-07-16 (dueña): en tercerizados SOLO Reyes (RY) y Ponce (AP).
    # Cualquier otro no-INTELA (R UNDA, GENERICA PRUEBAS, OFs sin código "?")
    # NO se muestra aparte: se SUMA a INTELA (KK) — su kg queda como autoprod.
    # Copiamos el of antes de tocarlo (prod puede venir del cache de Asinfo).
    _intela_ref = next((o for o in ofs if o.get("es_intela")), None)
    _int_cod = (_intela_ref or {}).get("cod") or "KK"
    _int_label = (_intela_ref or {}).get("label") or "INTELA"

    def _a_intela_si_desconocido(o: dict) -> dict:
        if (not o.get("es_intela")
                and (o.get("cod") or "").strip().upper() not in TERCERIZADOS_VALIDOS):
            o = dict(o)
            o["es_intela"] = True
            o["cod"] = _int_cod
            o["label"] = _int_label
        return o

    # TMT 2026-07-30 (dueña): "cuando tenemos un tejedor nuevo debería aparecer
    # una notificación". ANTES de mandarlos a INTELA, guardamos quiénes son —
    # si no, un tejedor que el programa no conoce desaparece EN SILENCIO dentro
    # de la producción propia. Fue exactamente lo que pasó con UN (Rodrigo Unda)
    # durante meses.
    _desc: dict = {}
    for o in ofs:
        if o.get("es_intela"):
            continue
        _c = (o.get("cod") or "").strip().upper()
        if _c and _c in TERCERIZADOS_VALIDOS:
            continue
        k = (o.get("label") or "?").strip()
        d = _desc.setdefault(k, {"label": k, "cod": _c, "ofs": 0, "kg": 0.0,
                                 "ejemplo": o.get("numero")})
        d["ofs"] += 1
        d["kg"] = round(d["kg"] + float(o.get("kg") or 0), 2)
    desconocidos = sorted(_desc.values(), key=lambda x: -x["kg"])

    ofs = [_a_intela_si_desconocido(o) for o in ofs]
    compras = _compras_k_por_prov(anio, mes) if disponible else {}
    estampadas = _ofts_estampadas() if disponible else {}
    # Tarifas $/kg por (proveedor, patrón de producto) — mig 0133. Se leen UNA
    # vez y se resuelven en memoria por fila (queries.resolver es pura).
    tarifas = _tarifas.listar_tarifas()

    # tejedores = columnas + match, ordenados por kg desc
    tej: dict = {}
    for of in ofs:
        k = _key(of)
        t = tej.setdefault(k, {
            "key": k, "cod": of["cod"], "label": of["label"],
            "es_intela": of["es_intela"], "ofs": 0, "kg": 0.0,
        })
        t["ofs"] += 1
        t["kg"] += of["kg"]
    tejedores = sorted(tej.values(), key=lambda x: -x["kg"])
    for t in tejedores:
        t["kg"] = round(t["kg"], 2)
        t["kg_of"] = t["kg"]  # producido por OFs cerradas (referencia diaria)
        comp = compras.get(t["cod"], {}) if t["cod"] else {}
        t["compra_kg"] = round(comp.get("kg", 0.0), 2)
        t["compra_importe"] = round(comp.get("importe", 0.0), 2)
        t["compra_n"] = comp.get("n", 0)
        t["falta_kg"] = round(t["kg"] - comp.get("kg", 0.0), 2)

    # ── TMT 2026-07-14 (dueña): "lo ingresado a tejeduría tiene que ser igual a
    # Ingresos crudo del flujo (114.126)". El TOTAL sale del INGRESO REAL a
    # bodega 52 (movimiento por saldo, corte 1° del mes) — la MISMA fuente que
    # el cuadro de movimientos del flujo, que cierra por telescopía con el stock.
    # Las OFs cerradas subcuentan (dejan afuera lo que está en máquina sin
    # cerrar). Reparto: los tercerizados quedan con sus kg de OF (para matchear
    # contra la compra que facturan); INTELA (autoprod, KK) = el PLUG que ata el
    # total al ingreso de bodega. Fail-soft: si Asinfo no da el ingreso, se deja
    # el total por OFs (comportamiento anterior).
    ingreso_bodega = 0.0
    if disponible:
        try:
            from datetime import date as _date
            _mov52 = asinfo_service.movimiento_bodega_mes(
                52, _date(int(anio), int(mes), 1)
            )
            ingreso_bodega = float((_mov52 or {}).get("ingreso") or 0.0)
        except Exception:  # noqa: BLE001 -- fail-soft
            ingreso_bodega = 0.0

    # Costo del hilo $/kg (para valuar el crudo de INTELA = hilo + 0,5, heurística
    # PRG/dBase UK=UM+0,5). Mismo $/kg que muestra el flujo (stock_act_ukg ≈ 2,954)
    # → coherente. Fail-soft: sin costo de hilo, INTELA queda sin $.
    hilo_ukg = 0.0
    if disponible:
        try:
            from modules.informes import queries as _inf_q
            _hil = (
                (_inf_q.movimientos_mes_dbase(anio, mes) or {}).get("header") or {}
            ).get("hilado") or {}
            hilo_ukg = float(_hil.get("stock_act_ukg")
                             or _hil.get("stock_inic_ukg") or 0.0)
        except Exception:  # noqa: BLE001 -- fail-soft
            hilo_ukg = 0.0
    crudo_intela_ukg = round(hilo_ukg + 0.5, 4) if hilo_ukg else 0.0

    ajustado = ingreso_bodega > 0
    # INTELA (autoprod) NO factura → no se mide contra una compra: es el RESIDUO.
    # Todo el crudo que entró a bodega 52 (= Ingresos crudo del panorama) menos
    # lo que trajeron los maquileros ES producción de INTELA. Así el total ata
    # al panorama/stock y los tercerizados quedan con sus kg (para matchear la
    # factura). Las OFs cerradas (kg_of) son solo el detalle diario (subconjunto).
    terc_kg = round(sum(t["kg"] for t in tejedores if not t["es_intela"]), 2)
    for t in tejedores:
        if t["es_intela"] and ajustado:
            t["kg"] = round(max(ingreso_bodega - terc_kg, 0.0), 2)
        # Costo: INTELA = kg × (hilo + 0,5); tercerizado = lo facturado ($/kg
        # sobre los kg mostrados para que $/kg × kg = $ en la fila).
        if t["es_intela"]:
            t["costo_kg"] = crudo_intela_ukg or None
            t["costo"] = (round(t["kg"] * crudo_intela_ukg, 2)
                          if crudo_intela_ukg else None)
        else:
            t["costo"] = round(t["compra_importe"], 2)
            t["costo_kg"] = (round(t["costo"] / t["kg"], 4)
                             if t["kg"] else None)

    total_kg_ajustado = (round(ingreso_bodega, 2) if ajustado
                         else round(sum(t["kg"] for t in tejedores), 2))
    total_costo = round(sum((t.get("costo") or 0.0) for t in tejedores), 2)

    # TMT 2026-07-16 (dueña): "poné las que faltan por cargar arriba de todo".
    # Re-orden: primero los tercerizados con falta_kg > 0 (los que hay que
    # cargar), mayor falta arriba; después el resto (INTELA autoprod y los ya
    # cargados) por kg desc. es_intela NUNCA es "pendiente" (autoprod, no
    # factura → su falta_kg no cuenta).
    def _orden_falta(t):
        falta = t.get("falta_kg") or 0.0
        pendiente = (not t["es_intela"]) and falta > 0.01
        return (0 if pendiente else 1,
                -(falta if pendiente else 0.0),
                -(t.get("kg") or 0.0))
    tejedores.sort(key=_orden_falta)

    # resumen diario (pivote por tejedor)
    dias: dict = {}
    for of in ofs:
        d = dias.setdefault(of["dia"], {"dia": of["dia"], "kg": {}, "total": 0.0})
        k = _key(of)
        d["kg"][k] = round(d["kg"].get(k, 0.0) + of["kg"], 2)
        d["total"] = round(d["total"] + of["kg"], 2)
    por_dia = sorted(dias.values(), key=lambda x: x["dia"], reverse=True)

    # ── Lista tercerizada POR OF (Reyes/Ponce) con estado + compra $ ──
    # Pedido dueña 2026-07-16:
    #  · columna "Compra $" que trae el importe de la compra cuando la
    #    encontramos (OFT estampado = match fino).
    #  · el botón "Cargar $" SOLO cuando no encontramos la compra.
    #  · meses PASADOS: todo "cargado" (no volvemos atrás a cargar junio).
    #  · tejedor ya cubierto (falta_kg<=0, cargó a la vieja sin estampar) →
    #    "cargado", sin botón (sino duplicaría).
    from filters import today_ec as _today_ec
    _hoy = _today_ec()
    es_mes_pasado = (int(anio), int(mes)) < (_hoy.year, _hoy.month)
    falta_por_cod = {t["cod"]: (t.get("falta_kg") or 0.0)
                     for t in tejedores if t.get("cod")}
    # Compras tipeadas a mano (sin OFT en el concepto) de la ventana: sirven
    # para no marcar "pendiente" una OF que en realidad YA se pagó. Se consume
    # el match, así que se recorre en orden estable (día, OFT).
    _desde_v, _hasta_v = _rango_ventana(anio, mes)
    a_mano_estado = _compras_k_a_mano(_desde_v, _hasta_v) if disponible else {}
    tercerizado_ofs = []
    for of in sorted(ofs, key=lambda o: (str(o.get("dia") or ""),
                                         str(o.get("numero") or ""))):
        if of["es_intela"]:
            continue
        numero = (of.get("numero") or "").upper()
        cargado = estampadas.get(numero) or {}
        monto = cargado.get("importe")   # $ de la compra si hay match fino
        kg_of = round(float(of.get("kg") or 0), 2)
        # ⭐ SALDO por OF (TMT 2026-08-19). El pasivo nace con los kilos, no
        # con el cierre, y una OF abierta sigue sumando: lo que falta cargar es
        # kg de la OF − kg ya cargado con ese OFT. Cuando la orden suma más
        # kilos, el saldo vuelve a ser > 0 y se carga UNA COMPRA NUEVA por la
        # diferencia (dueña: "una compra por el saldo nuevo") — nunca se toca
        # la que ya existe, para no pisar el importe si ella lo ajustó con la
        # factura real.
        kg_cargado = round(float(cargado.get("kg") or 0.0), 2)
        kg_saldo = round(kg_of - kg_cargado, 2)
        # Saldo NEGATIVO = se cargó más kg de los que Asinfo reconoce (la OF
        # bajó, o la compra se tipeó de más). No se resta solo: se avisa.
        #
        # ⚠ Pero sólo si la diferencia es MATERIAL. TMT 2026-08-19 (dueña,
        # viendo tres avisos el día del estreno: *"si vos las cargás, ¿cómo
        # puede ser que esté cargado de más?"*). Tenía razón en desconfiar: las
        # tres eran OFs de junio/julio que el motor cargó cuando cerraron y a
        # las que Asinfo después les ajustó el kilaje unos gramos — 3,22 kg
        # sobre 807,30 · 0,14 sobre 811,00 · 0,28 sobre 314,75. Nada que
        # corregir y tres avisos por nada. Se usa la MISMA tolerancia que el
        # match contra lo tipeado a mano (0,5%): ahí ya está medido que un
        # rollo pesado dos veces da 0,06% de diferencia.
        sobrecargada = kg_saldo < 0 and abs(kg_saldo) > TOLERANCIA_KG * max(
            kg_of, kg_cargado, 1.0)
        if kg_saldo <= 0.01:
            estado = "compra" if monto is not None else "cargado"
        elif es_mes_pasado and not of.get("oft"):
            # Mes viejo y fila que viene de una ORDEN cerrada (las de INGRESO
            # traen `oft`): cuando ese mes fue el mes en curso el motor ya la
            # vio y decidió. No volvemos atrás a cargar junio.
            # Los INGRESOS no entran acá a propósito: un IFT que llega tarde
            # sigue siendo un pasivo impago por viejo que sea el mes, y el
            # barrido de la ventana existe justamente para alcanzarlo.
            #
            # Va ANTES del match a mano a propósito: el match se CONSUME (una
            # factura tapa una sola OF), y si lo gastara una orden vieja que ya
            # estaba resuelta, la abierta que sí está en juego se quedaría sin
            # su cobertura y se cargaría dos veces.
            estado = "cargado"
        elif _match_a_mano(a_mano_estado, of.get("cod"), kg_of, of.get("dia")):
            estado = "cargado"         # la pagaron a mano, sin estampar el OFT
        elif kg_cargado > 0 or falta_por_cod.get(of.get("cod"), 0.0) > 0.01:
            estado = "pendiente"       # falta cargar (todo, o el saldo nuevo)
        else:
            estado = "cargado"         # el tejedor ya está cubierto en el mes
        # Tarifa e importe sugerido (TMT 2026-07-26): el $/kg sale de la tabla
        # de tarifas según el PRODUCTO de la OF (Ponce cobra distinto los HUF).
        # Sin tarifa → importe_sugerido None y NO se ofrece carga automática:
        # nunca inventamos un precio. El importe va sobre el SALDO, no sobre el
        # kg total: si ya se pagó una parte, sólo se carga lo que falta.
        _tar = _tarifas.resolver(tarifas, of.get("cod"), of.get("descripcion"))
        _kg_cobrable = kg_saldo if kg_saldo > 0.01 else 0.0
        tercerifa = {
            **of,
            "compra_monto": monto,
            "estado": estado,
            "tarifa": _tar,
            "kg_cargado": kg_cargado,
            "kg_saldo": kg_saldo,
            "sobrecargada": sobrecargada,
            "oft": of.get("oft") or "",
            "importe_sugerido": (round(_kg_cobrable * _tar, 2)
                                 if (_tar and _kg_cobrable) else None),
        }
        tercerizado_ofs.append(tercerifa)
    # TMT 2026-07-30 (dueña: "ordenalo por dia esto"). Antes agrupaba por tejedor
    # y dentro por día, así que para ver lo del día había que saltar entre dos
    # bloques. Ahora manda el DÍA, del más nuevo al más viejo — igual que el
    # diario de ingreso a bodega de la misma pantalla. El tejedor desempata.
    tercerizado_ofs.sort(
        key=lambda o: (str(o.get("dia") or ""), (o.get("cod") or ""),
                       str(o.get("numero") or "")),
        reverse=True,
    )
    pendientes = [o for o in tercerizado_ofs if o["estado"] == "pendiente"]

    # ── TMT 2026-07-21 (dueña): sumar columnas de tercerizados al diario de
    # ingreso a bodega. El desglose por tejedor sale de por_dia (OFs cerradas);
    # se cruza por día (YYYY-MM-DD) contra el ingreso a bodega 52. Aproximado: el
    # día de cierre de la OF puede no calzar exacto con el día de ingreso a bodega.
    #
    # ⭐ TMT 2026-08-07 (dueña: *"nos falta la columna para UN"*): las columnas
    # eran DOS hardcodeadas (Reyes y Ponce) y el INTELA del diario se calculaba
    # como `ingreso − Reyes − Ponce`. Cuando en julio entró UN (Unda), sus kg
    # quedaron adentro de la columna INTELA: el diario decía INTELA 328.690,09 y
    # el cuadro de arriba 327.443,94 — la diferencia eran los 1.246,15 kg de
    # Unda. Ahora las columnas salen de ORDEN_COLUMNAS_DIARIO ∪
    # TERCERIZADOS_VALIDOS y el residuo INTELA descuenta a TODOS, así las dos
    # tablas dicen el mismo INTELA y un tejedor nuevo no se esconde.
    _labels_tej = {t["cod"]: t["label"] for t in tejedores if t.get("cod")}
    _cods_diario = list(ORDEN_COLUMNAS_DIARIO) + sorted(
        TERCERIZADOS_VALIDOS - set(ORDEN_COLUMNAS_DIARIO))
    columnas_diario = [
        {"cod": _c,
         "label": _labels_tej.get(_c) or LABEL_TERCERIZADO.get(_c) or _c}
        for _c in _cods_diario
    ]
    _terc_dia = {str(_d.get("dia"))[:10]: (_d.get("kg") or {}) for _d in por_dia}
    ingreso_por_dia = _ingreso_por_dia(anio, mes)
    totales_diario = {"terc": {_c["cod"]: 0.0 for _c in columnas_diario},
                      "intela_kg": 0.0, "kg": 0.0}
    for _row in ingreso_por_dia:
        _kg = _terc_dia.get(str(_row.get("dia"))[:10], {})
        _row["terc_kg"] = {
            _c["cod"]: round(float(_kg.get(_c["cod"], 0.0) or 0.0), 2)
            for _c in columnas_diario
        }
        # INTELA (autoprod) = lo RESTANTE: ingreso a bodega − TODOS los
        # tercerizados. Así tercerizados + INTELA = Ingresado en cada fila, y la
        # suma del mes matchea el INTELA del cuadro de arriba. TMT 2026-07-21.
        _row["intela_kg"] = round(
            float(_row.get("kg", 0.0) or 0.0) - sum(_row["terc_kg"].values()), 2)
        # Alias de compatibilidad (los usa quien lea el dict desde afuera).
        _row["reyes_kg"] = _row["terc_kg"].get("RY", 0.0)
        _row["ponce_kg"] = _row["terc_kg"].get("AP", 0.0)
        for _c in columnas_diario:
            totales_diario["terc"][_c["cod"]] = round(
                totales_diario["terc"][_c["cod"]] + _row["terc_kg"][_c["cod"]], 2)
        totales_diario["intela_kg"] = round(
            totales_diario["intela_kg"] + _row["intela_kg"], 2)
        totales_diario["kg"] = round(
            totales_diario["kg"] + float(_row.get("kg", 0.0) or 0.0), 2)

    return {
        "disponible": disponible,
        "anio": prod.get("anio", anio),
        "mes": prod.get("mes", mes),
        # total = ingreso real a bodega 52 (coherente con "Ingresos crudo" del
        # flujo); si Asinfo no lo dio, cae al total por OFs.
        "total_kg": total_kg_ajustado,
        "total_kg_of": round(prod.get("total_kg", 0.0), 2),  # producido por OFs (detalle diario)
        "ingreso_bodega": round(ingreso_bodega, 2),
        "ajustado_a_bodega": ajustado,
        "hilo_ukg": round(hilo_ukg, 4),
        "crudo_intela_ukg": crudo_intela_ukg,
        "total_costo": total_costo,
        "tejedores": tejedores,
        "por_dia": por_dia,
        # DIARIO canónico (dueña 2026-07-20): ingreso a bodega 52 POR DÍA —
        # la suma de los días = total_kg exacto (misma fuente). Reemplaza en
        # la pantalla al diario por OFs cerradas (que sumaba 207k ≠ 179k).
        "ingreso_por_dia": ingreso_por_dia,
        # Columnas de tercerizados del diario (cod + label) y sus totales, para
        # que la plantilla no hardcodee ningún tejedor. TMT 2026-08-07.
        "columnas_diario": columnas_diario,
        "totales_diario": totales_diario,
        "pendientes": pendientes,
        "tercerizado_ofs": tercerizado_ofs,
        # Tarifas $/kg editables (mig 0133) + cuánto falta por proveedor.
        # `falta_por_cod` = del MES (lo que muestra la tabla por tejedor).
        # `falta_acum_por_cod` = ACUMULADO en la ventana = el TOPE real de la
        # carga automática (dueña 2026-07-26: "sí, por proveedor").
        "tarifas": tarifas,
        "falta_por_cod": falta_por_cod,
        "falta_acum_por_cod": (falta_acumulada(anio, mes) if disponible else {}),
        # Tejedores que Asinfo trae y el programa no reconoce (ver el aviso).
        "desconocidos": desconocidos,
        "meses_ventana_tope": MESES_VENTANA_TOPE,
        "total_pendiente": round(
            sum((o.get("importe_sugerido") or 0.0) for o in pendientes), 2),
        "pendientes_sin_tarifa": sum(
            1 for o in pendientes if o.get("importe_sugerido") is None),
    }


def _importes_k_existentes(desde: str, hasta: str) -> dict:
    """{cod_prov: [importes]} de las compras K vivas de la ventana — sólo para
    AVISAR de un posible duplicado en el preview (no bloquea).

    No bloquea a propósito: las facturas de Reyes repiten monto casi exacto (el
    rollo entero da ~1.630 siempre), así que descartar por importe tiraría OFs
    legítimas. La guarda que sí bloquea es el OFT estampado (y, desde el
    2026-07-30, el match por KG contra lo tipeado a mano — ver
    `_compras_k_a_mano`).
    """
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(COALESCE(codigo_prov, ''))) AS cod,
                   COALESCE(importe, 0) AS importe
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(stat, '') <> 'Y'
               AND fecha >= %s AND fecha < %s
            """,
            (desde, hasta),
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    out: dict = {}
    for r in rows:
        out.setdefault(r["cod"], []).append(float(r["importe"] or 0))
    return out


#: Tolerancia del match OF ↔ compra tipeada a mano. Reyes se pesa dos veces
#: (la hoja de Asinfo y la balanza de la factura) y da 810,52 vs 811,00: 0,06%.
#: Con 0,5% entran todos los pares reales y NO se toca el par más cercano que
#: es legítimamente distinto (821,78 vs 811,20 = 1,3%). Verificado sobre julio.
TOLERANCIA_KG = 0.005
#: Días de distancia máxima entre el cierre de la OF y la fecha de la factura.
DIAS_MATCH_A_MANO = 15


def _compras_k_a_mano(desde: str, hasta: str) -> dict:
    """{cod_prov: [{fecha, kg, numero}]} de las compras K TIPEADAS A MANO.

    ⚠ EL CASO QUE ESTO TAPA (descubierto 2026-07-30, verificando en vivo):
    Reyes y Ponce **se siguen cargando a mano desde la factura** — Tamara tipeó
    las 5 líneas de la factura 1253-1257 el 21/07, Andrés las de 1243-1249 el
    16/07 — pero esas compras NO llevan el OFT en el concepto, así que
    `_ofts_estampadas` no las ve y las OFs quedan "pendientes" para siempre. Con
    el tope sacado, el motor las habría vuelto a crear: **5 compras duplicadas
    de Reyes por ~$5.970 en julio**.

    Esto NO es un tope (no limita cuánto se puede cargar): es detección de
    DUPLICADO — "esta OF ya está cargada, sólo que sin el OFT". El match es por
    KG, que es la huella fuerte: la OF de 209,80 kg contra la compra de 209,80
    kg no es coincidencia.

    Se excluyen las que creó el propio motor (`usuario_crea = MARCADOR_CARGA`):
    esas ya llevan su OFT y las agarra `_ofts_estampadas`.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(COALESCE(codigo_prov, ''))) AS cod,
                   fecha, COALESCE(kg, 0) AS kg, numero
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(stat, '') <> 'Y'
               AND COALESCE(kg, 0) > 0
               AND COALESCE(usuario_crea, '') <> %s
               AND fecha >= %s AND fecha < %s
            """,
            (MARCADOR_CARGA, desde, hasta),
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    out: dict = {}
    for r in rows:
        out.setdefault(r["cod"], []).append(
            {"fecha": r["fecha"], "kg": float(r["kg"] or 0),
             "numero": r.get("numero")})
    return out


def _match_a_mano(a_mano: dict, cod: str, kg: float, dia) -> dict | None:
    """La compra tipeada a mano que YA cubre esta OF, o None.

    Consume el match (lo saca de la lista) para que una sola factura no tape
    dos OFs distintas: la factura de Reyes trae 5 líneas para 5 OFs.
    """
    if not kg:
        return None
    try:
        f_of = _parse_dia(dia)
    except Exception:  # noqa: BLE001
        return None
    candidatas = a_mano.get(cod) or []
    mejor, mejor_dif = None, None
    for c in candidatas:
        c_kg = c["kg"]
        if not c_kg:
            continue
        dif = abs(c_kg - kg) / max(c_kg, kg)
        if dif > TOLERANCIA_KG:
            continue
        c_fecha = c["fecha"]
        if hasattr(c_fecha, "date"):
            c_fecha = c_fecha.date()
        if abs((c_fecha - f_of).days) > DIAS_MATCH_A_MANO:
            continue
        if mejor_dif is None or dif < mejor_dif:
            mejor, mejor_dif = c, dif
    if mejor is not None:
        candidatas.remove(mejor)
    return mejor


def cargar_pendientes(anio: int, mes: int, *, usuario: str = "web",
                      clave: str | None = None, dry_run: bool = False) -> dict:
    """Crea las compras tipo K que faltan del mes, con kg de Asinfo × tarifa.

    Con `dry_run=True` NO escribe nada: devuelve el MISMO plan que ejecutaría.
    La pantalla de confirmación usa exactamente esta función, así que lo que se
    ve en el preview es literalmente lo que se va a crear (dueña 2026-07-26:
    "hacelo vos y fijate de no duplicar").

    TMT 2026-07-26 (pedido dueña: "que se carguen automáticamente"). Una compra
    por OF pendiente, estampando el OFT en el concepto para que el match fino de
    `_ofts_estampadas` la reconozca de acá en adelante.

    GUARDAS (en este orden — la carga ciega duplicaba):
      1. sólo OFs en estado 'pendiente', y por el SALDO (kg de Asinfo − kg ya
         cargado con ese OFT), nunca por el kg entero;
      2. **tarifa resuelta o se saltea** — nunca inventamos un precio;
      3. ~~tope acumulado por kg~~ — **SACADO el 2026-07-30** (dueña: "dejá de
         poner muchos topes que entorpece más que ayudar"). Era difuso y frenaba
         OFs reales. La guarda que queda es la EXACTA: `_ofts_estampadas`.
         (texto viejo, para contexto) las compras K viejas tipeadas a mano SIN kg hacen
         que la falta sobre-estime, y sin tope se crearían compras por OFs que
         ya están pagadas (caso real 26/07: 2 OFs de Reyes del 08/07). El tope
         es acumulado y no mensual porque el maquilero factura con desfase y a
         caballo de dos meses (dueña 2026-07-26: "sí, por proveedor"). Si Asinfo
         no responde, `falta_acumulada` devuelve {} y NO se carga nada.
         Se cargan las OFs más viejas primero hasta consumir la falta.
      4. `usuario_crea = 'asinfo-tejeduria'` — marcador que `scripts/import_dbf.py`
         PRESERVA en el sync del dBase (el DBF no tiene estas compras).

    Devuelve {creadas, importe, salteadas, detalle:[...]} y NO levanta si una OF
    falla: la anota en el detalle y sigue.
    """
    from modules.compras import queries as _compras_q

    data = resumen_mes(anio, mes)
    # Asinfo mudo → no se carga NADA. No es un tope: es no inventar producción
    # cuando el puente no contesta. `resumen_mes` marca `disponible=False` y sus
    # listas quedan vacías o incompletas.
    if not data.get("disponible"):
        return {"dry_run": dry_run, "creadas": 0, "importe": 0.0,
                "salteadas": 0, "restante": {}, "avisos_dup": 0,
                "detalle": [], "sin_asinfo": True}
    pendientes = data.get("pendientes") or []
    # Más viejas primero: si la falta no alcanza para todas, se cargan las que
    # llevan más tiempo sin facturar.
    pendientes = sorted(pendientes, key=lambda o: str(o.get("dia") or ""))
    # TOPE ACUMULADO por proveedor (no del mes) — dueña 2026-07-26.
    # TMT 2026-07-30 (dueña: "dejá de poner muchos topes que entorpece más que
    # ayudar"). El tope acumulado por kg SE FUE. Era una guarda difusa: comparaba
    # producción de una ventana de 3 meses contra las compras de esa misma
    # ventana, y como el maquilero factura con un mes de desfase frenaba OFs que
    # nadie había pagado (caso UN, julio). Se queda la guarda EXACTA, que es la
    # que importa: `_ofts_estampadas` — una OF con su OFT ya estampado en el
    # concepto no se vuelve a ofrecer, y eso no falla por desfase de fechas.
    # `restante` sigue calculándose sólo como DATO del plan, no frena nada.
    restante = falta_acumulada(anio, mes)
    estampadas = _ofts_estampadas()
    _desde, _hasta = _rango_ventana(anio, mes)
    existentes = _importes_k_existentes(_desde, _hasta)
    # Compras K tipeadas a mano (sin OFT en el concepto): si una de ellas ya
    # cubre la OF, no se vuelve a crear. Ver `_compras_k_a_mano`.
    a_mano = _compras_k_a_mano(_desde, _hasta)

    creadas, importe_total = 0, 0.0
    detalle: list[dict] = []
    for of in pendientes:
        cod = (of.get("cod") or "").upper().strip()
        numero = (of.get("numero") or "").upper()
        tarifa = of.get("tarifa")
        # Se carga el SALDO, no el kg total de la OF: lo que ya está pagado con
        # ese OFT no se vuelve a facturar. Se recalcula acá contra `estampadas`
        # fresco (y contra lo que este mismo lote va creando) para que dos
        # corridas seguidas no dupliquen. Ver el comentario de `_ofts_estampadas`.
        kg_of = round(float(of.get("kg") or 0), 2)
        kg_ya = round(float((estampadas.get(numero) or {}).get("kg") or 0.0), 2)
        kg = round(kg_of - kg_ya, 2)
        importe = round(kg * tarifa, 2) if (tarifa and kg > 0.01) else None
        # `doc` = el documento con el que se estampa la compra (desde el
        # 2026-08-19 el INGRESO, IFT-…; antes la orden, OFT-…). `orden` viaja
        # aparte para poder rastrear de qué orden salió ese ingreso.
        base = {"doc": numero, "orden": of.get("oft") or "",
                "cod": cod, "label": of.get("label") or "",
                "dia": of.get("dia"), "descripcion": of.get("descripcion") or "",
                "kg": kg, "kg_of": kg_of, "kg_cargado": kg_ya,
                "tarifa": tarifa, "importe": importe}

        if kg <= 0.01:
            detalle.append({**base, "ok": False, "motivo": (
                "ya tiene compra" if kg_ya else "OF sin kg")})
            continue
        if not tarifa or not importe:
            detalle.append({**base, "ok": False,
                            "motivo": f"sin tarifa para {cod or '?'}"})
            continue
        # ¿Ya la cargaron a mano desde la factura, sin estampar el OFT? Se
        # reconoce por los KG (huella fuerte) dentro de ±15 días. NO es un tope:
        # es no cargar dos veces la misma tela.
        ya = _match_a_mano(a_mano, cod, kg, of.get("dia"))
        if ya is not None:
            _n = ya.get("numero")
            detalle.append({**base, "ok": False,
                            "motivo": (f"ya está cargada a mano"
                                       f"{f' (compra {_n})' if _n else ''} · "
                                       f"{num_es(ya['kg'], 2)} kg")})
            continue
        # Aviso (no bloquea): ya hay una compra K de ese proveedor por el MISMO
        # importe en la ventana. Puede ser un duplicado o dos OFs del mismo peso.
        dup = any(abs(x - float(importe)) < 0.01 for x in existentes.get(cod, []))

        if dry_run:
            creadas += 1
            importe_total += float(importe)
            restante[cod] = round(restante.get(cod, 0.0) - kg, 2)
            _sumar_estampada(estampadas, numero, importe, kg)
            existentes.setdefault(cod, []).append(float(importe))
            detalle.append({**base, "ok": True, "dup_warn": dup})
            continue

        try:
            _fecha = _parse_dia(of.get("dia"))
            res = _compras_q.crear(
                fecha=_fecha,
                codigo_prov=cod,
                importe=importe,
                kg=kg,
                tipo="K",
                concepto=(f"{numero} {of.get('oft') or ''} "
                          f"{(of.get('descripcion') or '').strip()}"
                          ).replace("  ", " ").strip()[:200],
                clave=clave,
                usuario=MARCADOR_CARGA,
            )
        except Exception as e:  # noqa: BLE001 -- una OF que falla no corta el lote
            detalle.append({**base, "ok": False, "motivo": str(e)[:120]})
            continue

        creadas += 1
        importe_total += float(importe)
        restante[cod] = round(restante.get(cod, 0.0) - kg, 2)
        _sumar_estampada(estampadas, numero, importe, kg)
        existentes.setdefault(cod, []).append(float(importe))
        detalle.append({**base, "ok": True, "dup_warn": dup,
                        "numero_compra": res.get("numero")})

    out = {
        "dry_run": dry_run,
        "creadas": creadas,
        "importe": round(importe_total, 2),
        "salteadas": sum(1 for d in detalle if not d["ok"]),
        "avisos_dup": sum(1 for d in detalle if d["ok"] and d.get("dup_warn")),
        "restante": restante,
        "detalle": detalle,
    }
    # A la campanita, cargue quien cargue: el hilo de fondo, la pantalla, o el
    # botón. Nunca en dry_run (la pantalla de preview no avisa nada todavía).
    if creadas and not dry_run:
        _avisar_carga(out)
    return out


def _parse_dia(dia) -> date:
    """'YYYY-MM-DD' (o date) → date. La OF de Asinfo trae el día como texto."""
    if isinstance(dia, date):
        return dia
    return date.fromisoformat(str(dia)[:10])


def _ingreso_por_dia(anio: int, mes: int) -> list[dict]:
    """Ingreso diario a bodega 52 (fail-soft: [])."""
    try:
        from datetime import date as _date

        from modules.asinfo import service as _asvc
        return _asvc.ingreso_bodega_por_dia(52, _date(int(anio), int(mes), 1)) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return []


# ---------------------------------------------------------------------------
# NOVEDADES + corrida sola — TMT 2026-07-30 (dueña)
#
# 1. *"tejeduría tiene que correr sola"*: hasta hoy `cargar_pendientes` se
#    disparaba ÚNICAMENTE cuando alguien con permiso abría la pantalla. Si nadie
#    entraba, no se cargaba nada; si entraban tres personas, corría tres veces.
#    Ahora se cuelga del hilo de fondo (modules/_lib/autocarga_facturas.py) con
#    su propio freno de 30 minutos, y la pantalla queda como atajo.
# 2. *"hacé notificaciones globales"*: lo que carga se cuenta en la campanita.
#
# ⚠ Lo que NO se avisa, a propósito: los "kilos producidos y no comprados" de los
#    meses cerrados. Parece un pasivo sin registrar y NO lo es — las compras K de
#    PC arrancan en MAYO (las anteriores viven en el archivo del FoxPro), así que
#    la resta contra la producción de Asinfo, que tiene la historia completa, da
#    un número que se mueve 12.000 kg con sólo correr la ventana un mes. La dueña
#    hizo borrar la columna que lo mostraba el 30/07 ("nadie entiende") — ver
#    a7264f6. `falta_acumulada` queda como TOPE de la carga: es una guarda, no un
#    dato para mirar.
# ---------------------------------------------------------------------------

_AUTO_LOCK = threading.Lock()
_auto_ultimo_ts = 0.0
_AUTO_INTERVALO_MIN = 1800.0  # 30 min entre corridas de fondo

def _url_compras(cod: str, dias: list[str]) -> str:
    """El "ver →" del aviso: /compras filtrado por ESE tejedor y ESE mes.

    TMT 2026-08-19 (dueña): *"acá cuando pongo ver, mandame a compras filtrado
    por ese código de proveedor"*. Antes el link volvía a la pantalla de
    Producción Tejeduría, que muestra las OFs de Asinfo — no las compras que el
    aviso acaba de crear, que es lo que se quiere revisar.

    El rango va del día 1 del mes de la compra MÁS VIEJA al último día del mes
    de la MÁS NUEVA (la `fecha` de la compra es el día de la OF, no el de la
    carga): así el filtro contiene siempre todo lo cargado, aunque el lote
    quede a caballo de dos meses, y muestra el mes entero de contexto.

    ⚠ El link decide QUIÉN VE EL AVISO: la campanita muestra sólo lo que la
    persona puede abrir (modules/avisos/visibilidad.py). Al apuntar a /compras
    estos avisos pasan a pedir `compras.ver`, que INT no tiene desde el 05/08 —
    decisión de la dueña el 19/08: *"estos avisos son de compras"*. Los otros
    avisos de tejeduría (tejedor nuevo, sin tarifa, cargado de más) siguen
    apuntando a la pantalla de tejeduría, y esos INT los sigue viendo.
    """
    base = f"/compras?codigo={cod}"
    dias = sorted(d for d in (dias or []) if d)
    if not dias:
        return base
    d0 = date.fromisoformat(dias[0]).replace(day=1)
    d1 = date.fromisoformat(dias[-1])
    fin = (date(d1.year + 1, 1, 1) if d1.month == 12
           else date(d1.year, d1.month + 1, 1)) - timedelta(days=1)
    return f"{base}&desde={d0.isoformat()}&hasta={fin.isoformat()}"


def _avisar_carga(res: dict) -> int:
    """Un aviso por PROVEEDOR con lo que se acaba de cargar. Devuelve cuántos."""
    from modules.avisos import avisar as _avisar

    porprov: dict = {}
    for d in res.get("detalle") or []:
        if not d.get("ok"):
            continue
        cod = (d.get("cod") or "?").upper()
        acc = porprov.setdefault(
            cod, {"n": 0, "kg": 0.0, "importe": 0.0,
                  "label": d.get("label") or cod, "ofts": [], "dias": []})
        acc["n"] += 1
        acc["kg"] += float(d.get("kg") or 0)
        acc["importe"] += float(d.get("importe") or 0)
        acc["ofts"].append(str(d.get("doc") or ""))
        _d = str(d.get("dia") or "")[:10]
        if len(_d) == 10:
            acc["dias"].append(_d)

    puestos = 0
    for cod, a in porprov.items():
        # La clave es el conjunto exacto de OFs cargadas: si la corrida se
        # repite no entra de nuevo, y si carga una OF nueva sí.
        clave = f"tejeduria:{cod}:" + ",".join(sorted(a["ofts"]))
        puestos += bool(_avisar(
            fuente="tejeduria",
            titulo=(f"{a['label']} · $ {num_es(a['importe'], 2)}"),
            detalle=(f"Se cargaron {a['n']} compra"
                     f"{'' if a['n'] == 1 else 's'} · "
                     f"{num_es(a['kg'], 2)} kg"),
            importe=round(a["importe"], 2), cantidad=a["n"],
            url=_url_compras(cod, a["dias"]), clave=clave[:400],
        ))
    return puestos


def avisar_tejedores_nuevos(anio: int, mes: int) -> int:
    """Avisa por la campanita cuando falta algo para poder cargar un tejedor.

    TMT 2026-07-30 (dueña): *"cuando tenemos un tejedor nuevo, por ejemplo UN,
    debería aparecer una notificación que diga: vimos que hay un nuevo tejedor
    que produjo esta orden, cargás su tarifa así podemos proceder a cargar la
    compra… y un link para que vaya directo a esa parte donde dice tarifa"*.

    Dos situaciones distintas, y la diferencia importa porque una la resolvés
    vos sola y la otra no:

    · **SIN TARIFA** — el programa ya lo reconoce, sólo falta el $/kg. Se
      arregla en la misma pantalla: el link va derecho al tarifario.
    · **SIN RECONOCER** — Asinfo lo trae pero el programa no sabe qué proveedor
      es, así que su producción se está contando como propia. Eso NO se arregla
      desde la pantalla (el mapeo vive en el código) y el aviso lo dice.

    Idempotente por `clave`: mientras la situación no cambie no se repite, y si
    aparece una OF nueva del mismo tejedor vuelve a avisar con el conteo nuevo.
    Nunca levanta.
    """
    from modules.avisos import avisar as _avisar

    puestos = 0
    try:
        data = resumen_mes(anio, mes)
    except Exception as e:  # noqa: BLE001 -- el aviso nunca rompe al que avisa
        _LOG.warning("avisar_tejedores_nuevos: %s", e)
        return 0

    # 1) Tejedores que Asinfo trae y el programa NO reconoce.
    for d in data.get("desconocidos") or []:
        label = d.get("label") or "?"
        puestos += bool(_avisar(
            fuente="tejeduria",
            nivel="alerta",
            titulo=f"Tejedor sin reconocer: {label}",
            detalle=(f"Produjo {d['ofs']} orden{'' if d['ofs'] == 1 else 'es'} · "
                     f"{num_es(d['kg'], 2)} kg este mes y se está contando como "
                     f"producción propia. Hay que darlo de alta para poder "
                     f"cargarle las compras."),
            cantidad=d["ofs"],
            url="/produccion-tejeduria-asinfo",
            clave=f"tejeduria:sin-reconocer:{anio}-{mes:02d}:{label}:{d['ofs']}"[:400],
        ))

    # 2) Tejedores reconocidos con OFs esperando y SIN tarifa cargada.
    faltan: dict = {}
    for of in data.get("pendientes") or []:
        if of.get("tarifa"):
            continue
        cod = (of.get("cod") or "").strip().upper()
        if not cod:
            continue
        f = faltan.setdefault(cod, {"cod": cod, "label": of.get("label") or cod,
                                    "ofs": 0, "kg": 0.0})
        f["ofs"] += 1
        f["kg"] = round(f["kg"] + float(of.get("kg") or 0), 2)
    for f in faltan.values():
        puestos += bool(_avisar(
            fuente="tejeduria",
            nivel="alerta",
            titulo=f"Falta la tarifa de {f['label']}",
            detalle=(f"Tiene {f['ofs']} orden{'' if f['ofs'] == 1 else 'es'} · "
                     f"{num_es(f['kg'], 2)} kg esperando. Cargale el $/kg y la "
                     f"compra se carga sola."),
            cantidad=f["ofs"],
            url="/produccion-tejeduria-asinfo#tarifas",
            clave=f"tejeduria:sin-tarifa:{anio}-{mes:02d}:{f['cod']}:{f['ofs']}"[:400],
        ))

    # 3) OFs donde se cargó MÁS kg de los que Asinfo reconoce.
    #
    # ⭐ TMT 2026-08-19. Desde que la compra se crea con la OF ABIERTA, el kg
    # que se factura es el del momento y la orden todavía puede corregirse
    # hacia abajo. Si eso pasa, el saldo queda NEGATIVO: pagamos de más. El
    # programa NO lo arregla solo (una compra ya creada no se toca: puede
    # tener el importe ajustado a mano con la factura real) — avisa para que
    # se corrija por la pantalla de compras.
    for of in data.get("tercerizado_ofs") or []:
        if not of.get("sobrecargada"):
            continue
        _sobra = abs(float(of.get("kg_saldo") or 0.0))
        puestos += bool(_avisar(
            fuente="tejeduria",
            nivel="alerta",
            titulo=f"{of.get('label') or of.get('cod') or '?'} · cargado de más",
            detalle=(f"La {of.get('numero')} tiene "
                     f"{num_es(float(of.get('kg') or 0), 2)} kg en Asinfo y "
                     f"{num_es(float(of.get('kg_cargado') or 0), 2)} kg "
                     f"cargados en compras: {num_es(_sobra, 2)} kg de más. "
                     f"Ajustalo desde Editar en /compras."),
            cantidad=1,
            url="/produccion-tejeduria-asinfo",
            clave=(f"tejeduria:sobrecarga:{of.get('numero')}:"
                   f"{of.get('kg_saldo')}")[:400],
        ))
    return puestos


def _meses_a_barrer(hoy, meses: int = MESES_VENTANA_TOPE) -> list[tuple]:
    """[(anio, mes)] del mes en curso hacia atrás, del más viejo al más nuevo.

    Del más viejo al más nuevo para que el saldo de una orden se consuma en
    orden cronológico si la misma OF apareciera en dos ventanas.
    """
    out = []
    a, m = hoy.year, hoy.month
    for _ in range(max(1, int(meses))):
        out.append((a, m))
        a, m = (a - 1, 12) if m == 1 else (a, m - 1)
    return list(reversed(out))


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo: carga lo pendiente del mes y avisa.

    Respeta el switch de ambiente (TEJEDURIA_AUTO=0), el freno de 30 minutos, y
    todas las guardas de `cargar_pendientes` (Asinfo disponible, tarifa
    resuelta, OFT ya estampado). Nunca levanta.
    """
    global _auto_ultimo_ts
    res = {"corrio": False, "creadas": 0, "importe": 0.0, "avisos": 0}
    if os.environ.get("TEJEDURIA_AUTO", "1").strip() == "0":
        return res
    ahora = _time.monotonic()
    with _AUTO_LOCK:
        if _auto_ultimo_ts and (ahora - _auto_ultimo_ts) < _AUTO_INTERVALO_MIN:
            return res
        _auto_ultimo_ts = ahora
    try:
        from filters import today_ec

        hoy = today_ec()
        res["corrio"] = True
        # ⭐ BARRIDO HACIA ATRÁS (TMT 2026-08-19, dueña: *"se debería cargar
        # solo, no quiero un botón; como pasivos, igual que hacemos con
        # compras"*). El mes en curso no alcanza: una orden abierta en julio
        # que entregó kilos en julio nunca fue vista por el motor —sólo miraba
        # cerradas— y su pasivo no aparece por más que hoy sea agosto. Se
        # barren los mismos MESES_VENTANA_TOPE meses que usa el resto del
        # módulo. Lo que frena el duplicado NO es el mes: son las guardas de
        # `cargar_pendientes` (OFT estampado, saldo por OF, match por kg contra
        # lo tipeado a mano) más la regla de que en un mes cerrado sólo se
        # cargan las ÓRDENES ABIERTAS.
        # `cargar_pendientes` ya deja el aviso de lo que cargó (avisa igual si
        # lo dispara la pantalla), así que acá no se repite.
        res["avisos_alta"] = 0
        for _a, _m in _meses_a_barrer(hoy):
            carga = cargar_pendientes(_a, _m, usuario=MARCADOR_CARGA)
            res["creadas"] += carga.get("creadas") or 0
            res["importe"] = round(res["importe"] + (carga.get("importe") or 0.0), 2)
            # Lo que NO se pudo cargar también se avisa: un tejedor nuevo o sin
            # tarifa se quedaba esperando sin que nadie se enterara.
            res["avisos_alta"] += avisar_tejedores_nuevos(_a, _m)
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("tejeduría (fondo): %s", e)
    return res
