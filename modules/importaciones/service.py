"""Importaciones de Asinfo cruzadas contra las compras/anticipos del programa.

La lista "Importación" del ERP (Asinfo, vía Metabase) trae cada importación
con una `Nota` libre que termina con el código de la compra/anticipo del
programa: 2-3 letras (= `scintela.proveedor.codigo_prov`) + número
(= `scintela.compra.numero`). Ej: "ACMT/EXP/2026-27/8197 ( AC 36)".

Este módulo:
  1. Trae las importaciones de Asinfo (cantidad/fechas/total REFERENCIAL).
  2. Parsea el código de cada Nota (concepto_parser.parse_nota_importacion).
  3. Cruza ese código contra `scintela.compra` para traer el importe
     CONFIABLE en dólares (los dólares de Asinfo no son confiables).

Fail-soft: si Asinfo no responde devuelve []; si la DB del programa falla,
las importaciones igual se muestran sin el cruce.
"""
from __future__ import annotations

import logging

import db
from concepto_parser import parse_nota_importacion
from modules.asinfo import service as asinfo_service

_LOG = logging.getLogger("programa_core.importaciones")


def _numeros_de(code: dict) -> list[int]:
    """Lista de números que cubre un código (soporta rangos 'MH 64-65')."""
    n = code.get("numero")
    if n is None:
        return []
    hasta = code.get("numero_hasta")
    if hasta and hasta >= n:
        return list(range(n, hasta + 1))
    return [n]


def _buscar_compras(refs: set[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """{(codigo_prov, ref_num): fila de compra} para las refs pedidas.

    El número de la Nota de Asinfo (ej. "AC 98") NO es `scintela.compra.numero`
    (ese campo va vacío en estas compras de importación) sino el **`concepto`**
    de la compra: para los proveedores de importación (Ariescope, More Human,
    Aartimpex…) el concepto es justo ese número ("98", "16", …). Por eso
    cruzamos por `(codigo_prov, concepto-numérico)`. Verificado contra
    /compras en vivo 2026-06-09.

    Una sola query a scintela.compra. Fail-soft: {} si no hay refs o la DB falla.
    """
    if not refs:
        return {}
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            """
            SELECT id_compra,
                   UPPER(codigo_prov) AS codigo_prov,
                   NULLIF(regexp_replace(COALESCE(concepto, ''), '[^0-9]', '', 'g'), '')::int
                       AS ref_num,
                   numero, importe, tipo, comprobante, concepto,
                   TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha
              FROM scintela.compra
             WHERE UPPER(codigo_prov) = ANY(%s)
               AND NULLIF(regexp_replace(COALESCE(concepto, ''), '[^0-9]', '', 'g'), '')::int
                   = ANY(%s)
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("buscar_compras falló: %s", e)
        return {}
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        if r.get("ref_num") is None or r.get("codigo_prov") is None:
            continue
        key = (str(r["codigo_prov"]).strip().upper(), int(r["ref_num"]))
        # Si dos compras comparten (prov, concepto) — ej. importación partida —
        # acumulamos el importe en vez de pisar.
        if key in out:
            out[key] = {**out[key], "importe": float(out[key].get("importe") or 0) + float(r.get("importe") or 0)}
        else:
            out[key] = r
    return out


def _buscar_anticipos(refs: set[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """{(cta, ref_num): {importe, n}} de anticipos USD en scintela.dolares.

    Muchas importaciones se pagan como ANTICIPO USD (crédito/adelanto) y viven
    en `scintela.dolares` (cuenta = código de proveedor, concepto = nº de
    importación + tags CAE/SALDO/MAPFRE/INI). Recién se vuelven compra al
    "Convertir a compra". Acá sumamos todas las partidas por (cuenta, nº) para
    dar el USD del anticipo. Verificado contra /dolares en vivo 2026-06-09.

    Fail-soft: {} si no hay refs o la DB falla.
    """
    if not refs:
        return {}
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            r"""
            WITH d AS (
                SELECT UPPER(cta) AS cta, importe,
                       NULLIF(substring(concepto FROM '^\s*(\d{1,6})'), '')::int AS ref_num
                  FROM scintela.dolares
                 WHERE UPPER(cta) = ANY(%s)
            )
            SELECT cta, ref_num, SUM(importe) AS importe, COUNT(*) AS n
              FROM d
             WHERE ref_num IS NOT NULL AND ref_num = ANY(%s)
             GROUP BY cta, ref_num
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("buscar_anticipos falló: %s", e)
        return {}
    return {
        (str(r["cta"]).strip().upper(), int(r["ref_num"])): r
        for r in rows
        if r.get("ref_num") is not None and r.get("cta") is not None
    }


def promedios_usd_por_kg(provs: set[str]) -> dict[str, float]:
    """{PROV: promedio US$/kg} = Σimporte/Σkg de las compras previas del proveedor
    (kg>0). Para sugerir el costo estimado al recibir. Fail-soft: {} si la DB falla.
    """
    provs = sorted({(p or "").upper() for p in provs if p})
    if not provs:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(codigo_prov) AS prov,
                   SUM(importe) AS imp, SUM(kg) AS kg
              FROM scintela.compra
             WHERE UPPER(codigo_prov) = ANY(%s)
               AND COALESCE(kg, 0) > 0
               AND COALESCE(importe, 0) > 0
             GROUP BY UPPER(codigo_prov)
            """,
            (provs,),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("promedios_usd_por_kg falló: %s", e)
        return {}
    out: dict[str, float] = {}
    for r in rows:
        kg = float(r.get("kg") or 0)
        if kg > 0:
            out[str(r["prov"]).strip().upper()] = round(float(r.get("imp") or 0) / kg, 4)
    return out


def importaciones_con_cruce(limite: int = 400) -> list[dict]:
    """Importaciones de Asinfo enriquecidas con el código parseado y el cruce
    contra el programa: primero compra (`scintela.compra`), si no hay, anticipo
    USD (`scintela.dolares`).

    Cada fila agrega:
        codigo, prov, numero, numero_hasta
        compra            — None | {ids, first_id, importe_total, n, tipo}
        anticipo          — None | {importe_total, n}
        fuente            — 'compra' | 'anticipo' | None
        importe_programa  — float | None  (USD confiable del programa)
    """
    rows = asinfo_service.importaciones_asinfo(limite=limite)

    # Kg por importación (detalle Asinfo). Fail-soft: {} → columna vacía.
    try:
        kg_map = asinfo_service.importaciones_kg(limite=limite)
    except Exception:  # noqa: BLE001
        kg_map = {}
    # Costo estimado por TIPO DE HILADO (Σ promedio US$/kg del producto × kg).
    try:
        costo_hilado = asinfo_service.importaciones_costo_estimado(limite=limite)
    except Exception:  # noqa: BLE001
        costo_hilado = {}
    for r in rows:
        im = str(r.get("im_numero") or "").strip()
        r["kg"] = kg_map.get(im)
        r["_costo_hilado"] = costo_hilado.get(im)

    refs: set[tuple[str, int]] = set()
    for r in rows:
        code = parse_nota_importacion(r.get("nota"))
        r["codigo"] = code.get("codigo")
        r["prov"] = code.get("prov")
        r["numero"] = code.get("numero")
        r["numero_hasta"] = code.get("numero_hasta")
        if r["prov"] and r["numero"] is not None:
            for n in _numeros_de(code):
                refs.add((r["prov"], n))

    compras = _buscar_compras(refs)
    anticipos = _buscar_anticipos(refs)

    for r in rows:
        r["compra"] = None
        r["anticipo"] = None
        r["fuente"] = None
        r["importe_programa"] = None
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        keys = [(r["prov"], n) for n in _numeros_de(r)]

        hits = [compras[k] for k in keys if k in compras]
        if hits:
            r["compra"] = {
                "ids": [h["id_compra"] for h in hits],
                "first_id": hits[0]["id_compra"],
                "importe_total": sum(float(h["importe"] or 0) for h in hits),
                "n": len(hits),
                "tipo": (hits[0].get("tipo") or "").strip(),
            }
            r["fuente"] = "compra"
            r["importe_programa"] = r["compra"]["importe_total"]
            continue

        ahits = [anticipos[k] for k in keys if k in anticipos]
        if ahits:
            r["anticipo"] = {
                "importe_total": sum(float(h["importe"] or 0) for h in ahits),
                "n": sum(int(h["n"] or 0) for h in ahits),
            }
            r["fuente"] = "anticipo"
            r["importe_programa"] = r["anticipo"]["importe_total"]

    # ── Recepción / deuda / pago (PC-only, migraciones 0104+0107) ───────────
    # Keyed por (PROV, número primario). Flujo nuevo: RECIBIR (costo estimado →
    # deuda, kg al stock) → PAGAR (monto real sobrescribe deuda). El anticipo USD
    # aplicado netea la deuda. Ver modules/importaciones/pago.py.
    from modules.importaciones import pago as _pago

    # Sólo las importaciones con código (prov+nº) pueden recibirse/pagarse →
    # sólo de ésas pedimos estado (las sin código ni siquiera tienen botón).
    ims: set[str] = {
        str(r.get("im_numero") or "").strip()
        for r in rows
        if r.get("im_numero") and r.get("prov") and r.get("numero") is not None
    }
    estados = _pago.estados_por_im(ims)
    promedios = promedios_usd_por_kg({r.get("prov") for r in rows})

    for r in rows:
        r["recibido_pc"] = False
        r["kg_recibidos"] = None
        r["costo_estimado"] = None
        r["deuda"] = None
        r["pagada"] = False
        r["monto_real"] = None
        r["fecha_recepcion_pc"] = None
        r["fecha_pago"] = None
        r["anticipo_aplicado"] = 0.0
        r["necesita_costo_manual"] = False
        r["costo_ventana"] = None
        r["kg_sin_precio_hist"] = 0.0
        # Anticipo USD disponible para netear (si la importación tiene uno cruzado).
        r["anticipo_disponible"] = (
            float(r["anticipo"]["importe_total"]) if r.get("anticipo") else 0.0
        )
        # Sugerencia de costo estimado = Σ(promedio US$/kg por tipo de hilado × kg
        # de ese hilado en la importación). Fuente: detalle Asinfo. Si no hay
        # estimado por hilado, cae al promedio del proveedor como respaldo.
        ch = r.get("_costo_hilado")
        r["costo_ventana"] = None
        r["kg_sin_precio_hist"] = 0.0
        if ch and ch.get("costo"):
            r["costo_estimado_sugerido"] = round(float(ch["costo"]), 2)
            r["promedio_usd_kg"] = ch.get("usd_kg")
            r["costo_ventana"] = ch.get("ventana")
            r["kg_sin_precio_hist"] = float(ch.get("kg_sin_precio") or 0)
            # "que pregunte": si parte de los kg no tienen histórico 3m/6m,
            # marcamos que falta ingresar el costo a mano para esos hilados.
            r["necesita_costo_manual"] = (r["kg_sin_precio_hist"] or 0) > 0
        else:
            prom = promedios.get((r.get("prov") or "").upper())
            r["promedio_usd_kg"] = prom
            r["costo_estimado_sugerido"] = (
                round(prom * float(r["kg"]), 2) if prom and r.get("kg") else None
            )
            # Sin estimado por hilado ni promedio de proveedor → preguntar.
            r["necesita_costo_manual"] = r["costo_estimado_sugerido"] is None
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        st = estados.get(str(r.get("im_numero") or "").strip())
        if st:
            r["recibido_pc"] = bool(st["recibido_pc"])
            r["kg_recibidos"] = st["kg_recibidos"]
            r["costo_estimado"] = st["costo_estimado"]
            r["deuda"] = st["deuda"]
            r["pagada"] = bool(st["pagada"])
            r["monto_real"] = st["monto_real"]
            r["fecha_recepcion_pc"] = st["fecha_recepcion_pc"]
            r["fecha_pago"] = st["fecha_pago"]
            r["anticipo_aplicado"] = st["anticipo_aplicado"]
        # Estado legible del flujo nuevo.
        if r["pagada"]:
            r["estado_flujo"] = "pagada"
        elif r["recibido_pc"]:
            r["estado_flujo"] = "recibida"   # recibida, deuda pendiente
        else:
            r["estado_flujo"] = "en_transito"
    return rows


def kilos_pendientes_importaciones(limite: int = 400) -> dict:
    """Kilos de importaciones NO contabilizadas — para TC/PT.

    "Pendiente" = importación RECIBIDA (kg ya en stock) cuya DEUDA todavía no se
    pagó (recibido_pc y NOT pagada). Los kg salen de kg_recibidos (o el detalle
    de Asinfo como fallback).

    Devuelve {"kg": float, "n": int, "detalle": [filas]}. Fail-soft: kg 0.
    """
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("kilos_pendientes_importaciones falló: %s", e)
        return {"kg": 0.0, "n": 0, "detalle": []}
    pend = [
        r for r in rows
        if r.get("recibido_pc")
        and not r.get("pagada")
        and (r.get("kg_recibidos") or r.get("kg") or 0) > 0
    ]
    return {
        "kg": round(sum(float(r.get("kg_recibidos") or r.get("kg") or 0) for r in pend), 2),
        "n": len(pend),
        "detalle": pend,
    }
