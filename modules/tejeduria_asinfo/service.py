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
import re

import db
from modules.asinfo import service as asinfo_service

_OFT_RE = re.compile(r"OFT-\d+", re.IGNORECASE)

# Tercerizados válidos (pedido dueña 2026-07-16): en la tab SOLO mostramos
# Reyes (RY) y Ponce (AP). Cualquier otro no-INTELA (R UNDA, GENERICA PRUEBAS,
# OFs sin código que salen como "?") se excluye — no sabemos qué son / son
# tests. Para sumar un tercerizado nuevo, agregá su código de proveedor acá.
TERCERIZADOS_VALIDOS = {"RY", "AP"}


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

    ofs = [_a_intela_si_desconocido(o) for o in ofs]
    compras = _compras_k_por_prov(anio, mes) if disponible else {}
    estampadas = _ofts_estampadas() if disponible else {}

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
        tercerizado_ofs.append({**of, "compra_monto": monto, "estado": estado})
    tercerizado_ofs.sort(key=lambda o: ((o.get("cod") or ""), str(o.get("dia") or "")))
    pendientes = [o for o in tercerizado_ofs if o["estado"] == "pendiente"]

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
        "ingreso_por_dia": _ingreso_por_dia(anio, mes),
        "pendientes": pendientes,
        "tercerizado_ofs": tercerizado_ofs,
    }


def _ingreso_por_dia(anio: int, mes: int) -> list[dict]:
    """Ingreso diario a bodega 52 (fail-soft: [])."""
    try:
        from datetime import date as _date

        from modules.asinfo import service as _asvc
        return _asvc.ingreso_bodega_por_dia(52, _date(int(anio), int(mes), 1)) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return []
