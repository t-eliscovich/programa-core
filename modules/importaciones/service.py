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
    return rows
