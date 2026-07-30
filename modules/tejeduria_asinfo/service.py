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
from datetime import date

import db
from filters import num_es
from modules.asinfo import service as asinfo_service
from modules.tejeduria_asinfo import queries as _tarifas

_LOG = logging.getLogger("programa_core.tejeduria")

_OFT_RE = re.compile(r"OFT-\d+", re.IGNORECASE)

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
    """{OFT: $ compra} — OFT que figuran en el concepto de alguna compra tipo K
    (match fino: las cargadas desde esta tab), con el IMPORTE de la compra.

    Si una compra estampa varios OFT, se reparte el importe en partes iguales
    (en la práctica la tab crea 1 compra por OFT, así que es el importe entero).
    Sirve para mostrar la columna 'Compra $' en la lista por OF. Membership
    (`oft in estampadas`) sigue andando igual que antes (chequea las claves).
    """
    try:
        rows = db.fetch_all(
            """
            SELECT concepto, COALESCE(importe, 0) AS importe
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
        ofts = [m.upper() for m in _OFT_RE.findall(r.get("concepto") or "")]
        if not ofts:
            continue
        parte = float(r.get("importe") or 0) / len(ofts)
        for k in ofts:
            out[k] = round(out.get(k, 0.0) + parte, 2)
    return out


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
    ofs = prod.get("ofs", [])

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
    tercerizado_ofs = []
    for of in ofs:
        if of["es_intela"]:
            continue
        numero = (of.get("numero") or "").upper()
        monto = estampadas.get(numero)  # $ de la compra si hay match fino
        if monto is not None:
            estado = "compra"          # encontrada → muestra $
        elif es_mes_pasado:
            estado = "cargado"         # mes viejo → no se recarga
        elif falta_por_cod.get(of.get("cod"), 0.0) > 0.01:
            estado = "pendiente"       # falta y sin match → botón Cargar
        else:
            estado = "cargado"         # cubierto por match viejo del tejedor
        # Tarifa e importe sugerido (TMT 2026-07-26): el $/kg sale de la tabla
        # de tarifas según el PRODUCTO de la OF (Ponce cobra distinto los HUF).
        # Sin tarifa → importe_sugerido None y NO se ofrece carga automática:
        # nunca inventamos un precio.
        _tar = _tarifas.resolver(tarifas, of.get("cod"), of.get("descripcion"))
        tercerifa = {
            **of,
            "compra_monto": monto,
            "estado": estado,
            "tarifa": _tar,
            "importe_sugerido": (round(float(of.get("kg") or 0) * _tar, 2)
                                 if _tar else None),
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

    # ── TMT 2026-07-21 (dueña): sumar columnas Reyes (RY) y Ponce (AP) al diario
    # de ingreso a bodega. El desglose por tejedor sale de por_dia (OFs cerradas);
    # se cruza por día (YYYY-MM-DD) contra el ingreso a bodega 52. Aproximado: el
    # día de cierre de la OF puede no calzar exacto con el día de ingreso a bodega.
    _terc_dia = {str(_d.get("dia"))[:10]: (_d.get("kg") or {}) for _d in por_dia}
    ingreso_por_dia = _ingreso_por_dia(anio, mes)
    for _row in ingreso_por_dia:
        _kg = _terc_dia.get(str(_row.get("dia"))[:10], {})
        _row["reyes_kg"] = round(float(_kg.get("RY", 0.0) or 0.0), 2)
        _row["ponce_kg"] = round(float(_kg.get("AP", 0.0) or 0.0), 2)
        # INTELA (autoprod) = lo RESTANTE: ingreso a bodega − Reyes − Ponce.
        # Así Reyes+Ponce+INTELA = Ingresado en cada fila, y la suma del mes
        # matchea el INTELA del cuadro de arriba. TMT 2026-07-21 (dueña).
        _row["intela_kg"] = round(
            float(_row.get("kg", 0.0) or 0.0) - _row["reyes_kg"] - _row["ponce_kg"], 2)

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
      1. sólo OFs en estado 'pendiente' (ya excluye mes pasado y OFT estampado);
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
        kg = round(float(of.get("kg") or 0), 2)
        tarifa = of.get("tarifa")
        importe = of.get("importe_sugerido")
        base = {"oft": numero, "cod": cod, "label": of.get("label") or "",
                "dia": of.get("dia"), "descripcion": of.get("descripcion") or "",
                "kg": kg, "tarifa": tarifa, "importe": importe}

        if numero in estampadas:
            detalle.append({**base, "ok": False, "motivo": "ya tiene compra"})
            continue
        if not tarifa or not importe:
            detalle.append({**base, "ok": False,
                            "motivo": f"sin tarifa para {cod or '?'}"})
            continue
        if kg <= 0:
            detalle.append({**base, "ok": False, "motivo": "OF sin kg"})
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
            estampadas[numero] = float(importe)
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
                concepto=f"{numero} {(of.get('descripcion') or '').strip()}"[:200],
                clave=clave,
                usuario=MARCADOR_CARGA,
            )
        except Exception as e:  # noqa: BLE001 -- una OF que falla no corta el lote
            detalle.append({**base, "ok": False, "motivo": str(e)[:120]})
            continue

        creadas += 1
        importe_total += float(importe)
        restante[cod] = round(restante.get(cod, 0.0) - kg, 2)
        estampadas[numero] = float(importe)
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
                  "label": d.get("label") or cod, "ofts": []})
        acc["n"] += 1
        acc["kg"] += float(d.get("kg") or 0)
        acc["importe"] += float(d.get("importe") or 0)
        acc["ofts"].append(str(d.get("oft") or ""))

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
            url="/produccion-tejeduria-asinfo", clave=clave[:400],
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
    return puestos


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
        # `cargar_pendientes` ya deja el aviso de lo que cargó (avisa igual si
        # lo dispara la pantalla), así que acá no se repite.
        carga = cargar_pendientes(hoy.year, hoy.month, usuario=MARCADOR_CARGA)
        res["creadas"] = carga.get("creadas") or 0
        res["importe"] = carga.get("importe") or 0.0
        # Lo que NO se pudo cargar también se avisa: un tejedor nuevo o sin
        # tarifa se quedaba esperando sin que nadie se enterara.
        res["avisos_alta"] = avisar_tejedores_nuevos(hoy.year, hoy.month)
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("tejeduría (fondo): %s", e)
    return res
