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
import re

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


def _buscar_compras(refs: set[tuple[str, int]]) -> list[dict]:
    """Filas CRUDAS de compra (con `fecha`) para las refs pedidas.

    El número de la Nota de Asinfo (ej. "AC 98") NO es `scintela.compra.numero`
    (ese campo va vacío en estas compras de importación) sino el **`concepto`**
    de la compra: para los proveedores de importación (Ariescope, More Human,
    Aartimpex…) el concepto es justo ese número ("98", "16", …). Por eso
    cruzamos por `(codigo_prov, concepto-numérico)`. Verificado contra
    /compras en vivo 2026-06-09.

    Devuelve la LISTA de filas (no agregadas): cada una con codigo_prov, ref_num,
    importe y `fecha`. La atribución a cada importación (por año / fecha más
    cercana) la hace `importaciones_con_cruce`, porque el nº de concepto se reusa
    entre años y no se puede sumar por (prov, nº) a secas.

    Una sola query a scintela.compra. Fail-soft: [] si no hay refs o la DB falla.
    """
    if not refs:
        return []
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
        return []
    return [
        r for r in rows
        if r.get("ref_num") is not None and r.get("codigo_prov") is not None
    ]


def _buscar_anticipos(refs: set[tuple[str, int]]) -> list[dict]:
    """Filas CRUDAS de anticipos USD (con `fecha`) desde scintela.dolares.

    Muchas importaciones se pagan como ANTICIPO USD (crédito/adelanto) y viven
    en `scintela.dolares` (cuenta = código de proveedor, concepto = nº de
    importación + tags CAE/SALDO/MAPFRE/INI). Recién se vuelven compra al
    "Convertir a compra".

    NO agregamos por (cuenta, nº): el nº de concepto se REUSA cada año (hay un
    "AC 31" en 2024 y otro en 2026), así que sumar a secas mezcla importaciones
    de años distintos. Devolvemos cada partida con su `fecha` y la atribución a
    la importación correcta (por fecha más cercana) la hace
    `importaciones_con_cruce`. Verificado contra /dolares en vivo 2026-06-09.

    Fail-soft: [] si no hay refs o la DB falla.
    """
    if not refs:
        return []
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            r"""
            WITH d AS (
                SELECT UPPER(cta) AS cta, importe,
                       TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha,
                       -- nº del concepto como PALABRA: "31 SALDO"→31 y "AC 95"→95
                       -- (antes anclado al inicio: se perdían los que llevan el código adelante)
                       NULLIF(substring(concepto FROM '\y(\d{1,6})\y'), '')::int AS ref_num
                  FROM scintela.dolares
                 -- SOLO anticipos VIVOS (mismo criterio que /dolares "solo vivos").
                 -- Al convertir a compra el anticipo pasa a st='B' (y 'X' si se
                 -- cancela) → deja de contar acá, igual que sale de /dolares. Así
                 -- el cruce coincide con /dolares y no infla el valor del stock.
                 WHERE UPPER(cta) = ANY(%s)
                   AND COALESCE(st, '') IN ('', ' ')
            )
            SELECT cta, ref_num, importe, fecha
              FROM d
             WHERE ref_num IS NOT NULL AND ref_num = ANY(%s)
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("buscar_anticipos falló: %s", e)
        return []
    return [
        r for r in rows
        if r.get("ref_num") is not None and r.get("cta") is not None
    ]


def _to_date(s):
    """'YYYY-MM-DD…' → date | None (fail-soft)."""
    from datetime import date
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


_ATRIB_MAX_DIAS = 300  # ventana movimiento↔importación (< 365 = separa años)


def _nearest_import(cands: list[dict], fecha_row) -> dict | None:
    """De las importaciones que comparten (prov, nº), la de fecha más cercana a
    la del movimiento (anticipo/compra), SOLO si cae dentro de la ventana.

    Desambigua el nº de concepto reusado entre años: cada anticipo/compra se
    atribuye a SU importación (la del mismo año/fecha), no a todas las que
    comparten ese número. La ventana (`_ATRIB_MAX_DIAS` < 365) evita que un
    movimiento de un año se pegue a la importación de otro año cuando la
    importación de su propio año no vino en el set (ej. `limite` corto) →
    devuelve None (no se atribuye) en vez de contaminar. Sin fecha en el
    movimiento, solo se atribuye si hay una única candidata.
    """
    fr = _to_date(fecha_row)
    if fr is None:
        return cands[0] if len(cands) == 1 else None
    best = None
    best_d = None
    for c in cands:
        fi = _to_date(c.get("fecha"))
        if fi is None:
            continue
        d = abs((fi - fr).days)
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is None:  # ninguna candidata tenía fecha
        return cands[0] if len(cands) == 1 else None
    return best if best_d <= _ATRIB_MAX_DIAS else None


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
    for r in rows:
        im = str(r.get("im_numero") or "").strip()
        r["kg"] = kg_map.get(im)

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

    # Índice (prov, nº) → importaciones que usan ese código, para atribuir cada
    # anticipo/compra a SU importación por fecha (el nº se reusa entre años).
    imports_by_ref: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        if r.get("prov") and r.get("numero") is not None:
            for n in _numeros_de(r):
                imports_by_ref.setdefault(
                    (str(r["prov"]).strip().upper(), n), []
                ).append(r)

    compras = _buscar_compras(refs)
    anticipos = _buscar_anticipos(refs)

    # Atribución: cada fila cruda (compra/anticipo) va a la importación de fecha
    # más cercana entre las que comparten su (prov, nº). Acumulamos por im_numero.
    comp_por_im: dict[str, list[dict]] = {}
    for c in compras:
        cands = imports_by_ref.get(
            (str(c.get("codigo_prov")).strip().upper(), int(c["ref_num"]))
        )
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:  # fuera de la ventana → no es de ninguna de estas IM
            continue
        comp_por_im.setdefault(im["im_numero"], []).append(c)

    ant_por_im: dict[str, list[dict]] = {}
    for a in anticipos:
        cands = imports_by_ref.get(
            (str(a.get("cta")).strip().upper(), int(a["ref_num"]))
        )
        if not cands:
            continue
        im = _nearest_import(cands, a.get("fecha"))
        if im is None:  # fuera de la ventana → no es de ninguna de estas IM
            continue
        ant_por_im.setdefault(im["im_numero"], []).append(a)

    for r in rows:
        r["compra"] = None
        r["anticipo"] = None
        r["fuente"] = None
        r["importe_programa"] = None
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        im = str(r.get("im_numero") or "").strip()

        hits = comp_por_im.get(im, [])
        if hits:
            r["compra"] = {
                "ids": [h["id_compra"] for h in hits],
                "first_id": hits[0]["id_compra"],
                "importe_total": sum(float(h.get("importe") or 0) for h in hits),
                "n": len(hits),
                "tipo": (hits[0].get("tipo") or "").strip(),
            }
            r["fuente"] = "compra"
            r["importe_programa"] = r["compra"]["importe_total"]
            continue

        ahits = ant_por_im.get(im, [])
        if ahits:
            r["anticipo"] = {
                "importe_total": sum(float(h.get("importe") or 0) for h in ahits),
                "n": len(ahits),
                # Desglose de los anticipos de /dólares que forman este total —
                # para que el "Ver" muestre las N partidas (antes mostraba solo
                # los movimientos cargados por esta pantalla). Más nuevo arriba.
                "items": [
                    {"fecha": h.get("fecha"),
                     "importe": float(h.get("importe") or 0)}
                    for h in sorted(
                        ahits, key=lambda x: str(x.get("fecha") or ""), reverse=True
                    )
                ],
            }
            r["fuente"] = "anticipo"
            r["importe_programa"] = r["anticipo"]["importe_total"]

    # ── Recepción / anticipos (PC-only, migs 0104+0107+0113) ────────────────
    # Modelo v2 — TMT 2026-07-06 (dueña): "dejamos de predecir cuánto saldría".
    # Sin flujo "Pagar" ni costo estimado: RECIBIR mete los kg al stock, los
    # ANTICIPOS son MOVIMIENTOS (muchos por importación, nada se pisa, ND
    # automática en Pichincha) y el VALOR DEL STOCK de la importación =
    # Σ anticipos pagados. El RESTANTE se carga por /compras → posdat, que es
    # el pasivo real. Ver modules/importaciones/pago.py.
    from modules.importaciones import pago as _pago

    # Sólo las importaciones con código (prov+nº) entran al flujo PC → sólo
    # de ésas pedimos estado/movimientos (las sin código no tienen botones).
    ims: set[str] = {
        str(r.get("im_numero") or "").strip()
        for r in rows
        if r.get("im_numero") and r.get("prov") and r.get("numero") is not None
    }
    estados = _pago.estados_por_im(ims)
    movs_map = _pago.movimientos_por_im(ims)

    for r in rows:
        r["recibido_pc"] = False
        r["kg_recibidos"] = None
        r["fecha_recepcion_pc"] = None
        r["movimientos"] = []
        # Σ anticipos pagados = VALOR DEL STOCK de la importación (v2).
        r["anticipo_aplicado"] = 0.0
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        im = str(r.get("im_numero") or "").strip()
        st = estados.get(im)
        if st:
            r["recibido_pc"] = bool(st["recibido_pc"])
            r["kg_recibidos"] = st["kg_recibidos"]
            r["fecha_recepcion_pc"] = st["fecha_recepcion_pc"]
            r["anticipo_aplicado"] = float(st["anticipo_aplicado"] or 0)
        movs = movs_map.get(im, [])
        r["movimientos"] = movs
        if movs:
            # Los movimientos son la fuente de verdad; el cache
            # anticipo_aplicado podría estar viejo justo tras la mig 0113.
            r["anticipo_aplicado"] = round(
                sum(float(m.get("monto") or 0) for m in movs), 2
            )
    return rows


def costo_hilado_recibido_mes(yy: int, mm: int, limite: int = 1000) -> dict:
    """Costo-a-la-fecha en USD del hilado RECIBIDO en el mes (yy, mm), tomado de
    NUESTRA base: anticipos (`scintela.dolares`) + compras (`scintela.compra`),
    cruzados por código+concepto y ATRIBUIDOS por año (fecha más cercana) para no
    arrastrar el mismo nº de concepto de otro año.

    Mismo universo de importaciones que `asinfo_service.hilado_recibido_mes`
    (recibidas por fecha de recepción). El $ se cuenta UNA sola vez por
    (prov, nº, año): las importaciones partidas (---1/---2) comparten el costo.
    Es "costo-a-la-fecha": crece solo a medida que cargan CAE/saldo/etc.

    Returns:
        {"us": float, "kg": float, "usd_kg": float | None}. Fail-soft: ceros.
    """
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper la vista
        return {"us": 0.0, "kg": 0.0, "usd_kg": None}
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    total_us = 0.0
    total_kg = 0.0
    vistos: set[tuple] = set()
    for r in rows or []:
        if not r.get("recibida"):
            continue
        if not str(r.get("fecha_recepcion") or "").startswith(pref):
            continue
        total_kg += float(r.get("kg") or 0.0)
        imp = r.get("importe_programa")
        if not imp:
            continue
        clave = (
            str(r.get("prov") or "").strip().upper(),
            r.get("numero"),
            str(r.get("fecha") or "")[:4],
        )
        if clave in vistos:
            continue
        vistos.add(clave)
        total_us += float(imp)
    usd_kg = round(total_us / total_kg, 4) if total_kg else None
    return {"us": round(total_us, 2), "kg": round(total_kg, 2), "usd_kg": usd_kg}


def promedio_hilado_usd_kg(limite: int = 1000) -> float | None:
    """$/kg promedio ponderado del hilado importado, TODO de Programa Core:
    Σ(costo real = importe_programa: anticipos+compras) ÷ Σ(kg) de las
    importaciones RECIBIDAS y cruzadas. Es el costo con que se valúa la apertura
    del stock en el Flujo producción, sin depender del dBase.

    El $ se cuenta UNA vez por (prov, nº, año) — importaciones partidas comparten
    costo; los kg se suman de todas las filas recibidas. Fail-soft: None si Asinfo
    cae o no hay datos.
    """
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001
        return None
    total_us = 0.0
    total_kg = 0.0
    vistos: set[tuple] = set()
    for r in rows or []:
        if not r.get("recibida"):
            continue
        kg = float(r.get("kg") or 0.0)
        imp = r.get("importe_programa")
        if not kg or not imp:
            continue
        total_kg += kg
        clave = (str(r.get("prov") or "").upper(), r.get("numero"),
                 str(r.get("fecha") or "")[:4])
        if clave not in vistos:
            vistos.add(clave)
            total_us += float(imp)
    return round(total_us / total_kg, 4) if total_kg else None


def _index_importaciones_por_codigo(limite: int = 400) -> dict[tuple[str, int], list[dict]]:
    """{(prov_upper, numero) -> [importación rows]} desde Asinfo, con kg colgado.

    Cada importación trae im_numero, fecha, fecha_recepcion, recibida, kg. Se usa
    para atribuir por (código, fecha más cercana). Fail-soft: {} si Asinfo cae.
    """
    try:
        rows = asinfo_service.importaciones_asinfo(limite=limite)
        kg_map = asinfo_service.importaciones_kg(limite=limite)
    except Exception:  # noqa: BLE001 -- Asinfo/Metabase caído
        return {}
    index: dict[tuple[str, int], list[dict]] = {}
    for r in rows or []:
        code = parse_nota_importacion(r.get("nota"))
        prov, numero = code.get("prov"), code.get("numero")
        if not prov or numero is None:
            continue
        r["kg"] = kg_map.get(str(r.get("im_numero") or "").strip())
        for n in _numeros_de(code):  # soporta rangos "MH 64-65"
            index.setdefault((str(prov).strip().upper(), n), []).append(r)
    return index


def adjuntar_recepcion_asinfo(anticipos: list[dict], limite: int = 400) -> None:
    """Muta cada fila de anticipo (scintela.dolares) agregando la RECEPCIÓN de su
    importación de Asinfo: `im_numero`, `fecha_recepcion_im`, `kg_im`.

    Match por (cta, concepto-numérico) + importación de fecha más cercana (misma
    ventana de 300 días que importaciones_con_cruce, así el "31" de 2024 no se
    pega a la importación de 2026). El concepto del anticipo es tipo "31 SALDO":
    el prefijo numérico es el código. Fail-soft: si Asinfo cae, deja los campos en
    None y NUNCA rompe /dolares.
    """
    for a in anticipos or []:  # init siempre → columna estable aunque Asinfo caiga
        a["im_numero"] = None
        a["fecha_recepcion_im"] = None
        a["kg_im"] = None
        _mref = re.search(r"\b(\d{1,6})\b", a.get("concepto") or "")  # "31 SALDO"→31, "AC 95"→95
        a["ref"] = int(_mref.group(1)) if _mref else None
    if not anticipos:
        return
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return
    for a in anticipos:
        cta = (a.get("cta") or "").strip().upper()
        m = re.search(r"\b(\d{1,6})\b", a.get("concepto") or "")  # nº aunque el código vaya adelante
        if not cta or not m:
            continue
        cands = index.get((cta, int(m.group(1))))
        if not cands:
            continue
        im = _nearest_import(cands, a.get("fecha"))  # ventana 300 días
        if im is None:
            continue
        a["im_numero"] = im.get("im_numero")
        a["fecha_recepcion_im"] = im.get("fecha_recepcion")
        a["kg_im"] = im.get("kg")


def kg_stock_por_compra(compras: list[dict], limite: int = 400) -> dict[str, float]:
    """{prov_upper -> kg total de Asinfo} para compras de importación.

    Cada compra trae `prov`, `ref` (nº del concepto) y `fecha`. El kg pertenece
    al STOCK (la importación de Asinfo), NO a la compra: muchas compras
    (SALDO/CAE/seguro) mapean a un solo stock, así que el kg se cuenta UNA vez
    por importación (dedup por im_numero) y se suma por proveedor. Atribución por
    fecha más cercana (ventana 300 días). Fail-soft: {} si Asinfo cae.
    """
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return {}
    por_prov: dict[str, float] = {}
    vistos: dict[str, set] = {}
    for c in compras or []:
        prov = str(c.get("prov") or "").strip().upper()
        ref = c.get("ref")
        if not prov or ref is None:
            continue
        cands = index.get((prov, int(ref)))
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:
            continue
        imn = im.get("im_numero")
        seen = vistos.setdefault(prov, set())
        if imn in seen:  # muchas compras → un stock: el kg una sola vez
            continue
        seen.add(imn)
        por_prov[prov] = por_prov.get(prov, 0.0) + float(im.get("kg") or 0.0)
    return por_prov


def adjuntar_kg_asinfo_a_compras(compras: list[dict], limite: int = 400) -> None:
    """Muta cada compra agregando `kg_asinfo`: el kg de su importación de Asinfo
    (match por codigo_prov + concepto-numérico + fecha más cercana).

    Para las compras de importación (BAP) que quedan con kg 0 — el kg vive en el
    STOCK, acá se MUESTRA por referencia (dueña: "cuando se convierte a compra
    quiero ver los kg"). No escribe nada en la compra. Fail-soft: kg_asinfo=None
    si Asinfo cae o el concepto no matchea una importación.
    """
    for c in compras or []:
        c["kg_asinfo"] = None
    if not compras:
        return
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return
    for c in compras:
        prov = (c.get("codigo_prov") or "").strip().upper()
        m = re.search(r"\b(\d{1,6})\b", c.get("concepto") or "")
        if not prov or not m:
            continue
        cands = index.get((prov, int(m.group(1))))
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:
            continue
        c["kg_asinfo"] = im.get("kg")
