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


def importaciones_con_cruce(limite: int = 400) -> list[dict]:
    """Importaciones de Asinfo enriquecidas con el código parseado y el
    cruce contra scintela.compra.

    Cada fila agrega:
        codigo        — "AC 36" | None
        prov, numero, numero_hasta
        compra        — None, o dict {ids, first_id, importe_total, n, tipo}
                        con el/los registro(s) del programa que matchearon.
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

    matches = _buscar_compras(refs)

    for r in rows:
        r["compra"] = None
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        keys = [(r["prov"], n) for n in _numeros_de(r)]
        hits = [matches[k] for k in keys if k in matches]
        if hits:
            r["compra"] = {
                "ids": [h["id_compra"] for h in hits],
                "first_id": hits[0]["id_compra"],
                "importe_total": sum(float(h["importe"] or 0) for h in hits),
                "n": len(hits),
                "tipo": (hits[0].get("tipo") or "").strip(),
            }
    return rows
