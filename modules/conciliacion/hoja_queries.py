"""Hoja de conciliación imprimible.

Formato T-account clásico:

    SALDO INICIAL (PC al desde-1)               $A

    I. MOVS CONCILIADOS DEL PERÍODO
       + Depósitos conciliados                  $B
          [lista: fecha doc num detalle cliente importe]
       − Débitos conciliados                    $C
          [lista]
       = SALDO CONCILIADO                       A + B − C = $D

    II. PENDIENTES AL CIERRE
       + Depósitos no conciliados               $E
          [lista, incluye históricos pendientes]
       − Débitos no conciliados                 $F
          [lista]
       = SALDO FINAL ESPERADO                   D + E − F = $G

    SALDO SEGÚN REGISTRO DEL BANCO              $H
    DIFERENCIA                                  G − H

TMT 2026-05-28 — dueña pidió esto en papel.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import db as _db


# Documentos que suman al saldo (créditos) vs los que restan (débitos).
# Misma matriz que el resto de modules/conciliacion/*.py.
DOCS_CREDITO = ("DE", "TR", "XX", "NC", "IN", "AC")
DOCS_DEBITO = ("CH", "ND", "DB", "GS", "PA")


def _fmt(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def saldo_pc_anterior(no_banco: int, antes_de: date) -> float:
    """Saldo PC libros al cierre del día anterior a `antes_de`.

    Es el saldo running del último mov con fecha < antes_de.
    """
    row = _db.fetch_one(
        """
        SELECT t.saldo
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %(no_banco)s
           AND t.fecha < %(antes_de)s
         ORDER BY t.fecha DESC, t.id_transaccion DESC
         LIMIT 1
        """,
        {"no_banco": no_banco, "antes_de": antes_de},
    )
    return _fmt(row.get("saldo") if row else 0)


def saldo_pc_al(no_banco: int, hasta: date) -> float:
    """Saldo PC libros al cierre de la fecha `hasta` (running balance)."""
    row = _db.fetch_one(
        """
        SELECT t.saldo
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %(no_banco)s
           AND t.fecha <= %(hasta)s
         ORDER BY t.fecha DESC, t.id_transaccion DESC
         LIMIT 1
        """,
        {"no_banco": no_banco, "hasta": hasta},
    )
    return _fmt(row.get("saldo") if row else 0)


_SELECT_TXBANCO_BASE = """
    SELECT
        t.id_transaccion,
        t.fecha,
        t.documento,
        t.concepto,
        t.numreferencia,
        t.importe,
        t.prov,
        t.stat,
        COALESCE(c.nombre, p.nombre_prov, '') AS contraparte
      FROM scintela.transacciones_bancarias t
      LEFT JOIN scintela.cliente c
             ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(t.prov))
      LEFT JOIN scintela.proveedor p
             ON UPPER(TRIM(p.codigo_prov)) = UPPER(TRIM(t.prov))
     WHERE t.no_banco = %(no_banco)s
       AND t.fecha BETWEEN %(desde)s AND %(hasta)s
"""

_COND_CONCILIADO = (
    " AND ("
    "      TRIM(COALESCE(t.stat,'')) = '*'"
    "   OR EXISTS ("
    "        SELECT 1 FROM scintela.banco_conciliacion_match m"
    "         WHERE m.id_transaccion = t.id_transaccion"
    "           AND m.deshecho_en IS NULL"
    "   )"
    " )"
)

_COND_PENDIENTE = (
    " AND TRIM(COALESCE(t.stat,'')) <> '*'"
    " AND NOT EXISTS ("
    "        SELECT 1 FROM scintela.banco_conciliacion_match m"
    "         WHERE m.id_transaccion = t.id_transaccion"
    "           AND m.deshecho_en IS NULL"
    "    )"
)


def _movs(
    no_banco: int, desde: date, hasta: date, *, conciliados: bool, docs: tuple[str, ...]
) -> list[dict[str, Any]]:
    doc_list = ", ".join(f"'{d}'" for d in docs)
    cond = _COND_CONCILIADO if conciliados else _COND_PENDIENTE
    sql = (
        _SELECT_TXBANCO_BASE
        + cond
        + f" AND UPPER(TRIM(t.documento)) IN ({doc_list})"
        + " ORDER BY t.fecha, t.id_transaccion"
    )
    rows = _db.fetch_all(sql, {"no_banco": no_banco, "desde": desde, "hasta": hasta}) or []
    out = []
    for r in rows:
        out.append({
            "id_transaccion": r.get("id_transaccion"),
            "fecha": r.get("fecha"),
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or "").strip(),
            "numero": (r.get("numreferencia") or "").strip(),
            "importe": _fmt(r.get("importe")),
            "prov": (r.get("prov") or "").strip(),
            "contraparte": (r.get("contraparte") or "").strip(),
            "stat": (r.get("stat") or "").strip(),
        })
    return out


def historicos_pendientes_creditos(no_banco: int) -> list[dict[str, Any]]:
    """Depósitos históricos pendientes (cargados de Excel viejos), no conciliados.

    Aparecen siempre como pendientes hasta que se concilien.
    """
    rows = _db.fetch_all(
        """
        SELECT id, fecha, concepto, documento, monto AS importe, tipo, detalle
          FROM scintela.banco_historicos_pendientes
         WHERE no_banco = %(no_banco)s
           AND conciliado_en IS NULL
           AND COALESCE(tipo, 'C') = 'C'
         ORDER BY fecha, id
        """,
        {"no_banco": no_banco},
    ) or []
    out = []
    for r in rows:
        out.append({
            "id_historico": r.get("id"),
            "fecha": r.get("fecha"),
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or "").strip(),
            "numero": (r.get("documento") or "").strip(),  # documento = nro comprobante banco
            "importe": _fmt(r.get("importe")),
            "prov": "",
            "contraparte": "",
            "stat": "hist",
            "es_historico": True,
        })
    return out


def hoja_conciliacion(no_banco: int, desde: date, hasta: date) -> dict[str, Any]:
    """Arma toda la hoja de conciliación lista para renderizar."""
    saldo_inicial = saldo_pc_anterior(no_banco, desde)

    creditos_conc = _movs(no_banco, desde, hasta, conciliados=True, docs=DOCS_CREDITO)
    debitos_conc = _movs(no_banco, desde, hasta, conciliados=True, docs=DOCS_DEBITO)
    creditos_pend = _movs(no_banco, desde, hasta, conciliados=False, docs=DOCS_CREDITO)
    debitos_pend = _movs(no_banco, desde, hasta, conciliados=False, docs=DOCS_DEBITO)
    historicos = historicos_pendientes_creditos(no_banco)

    sum_cred_conc = round(sum(m["importe"] for m in creditos_conc), 2)
    sum_deb_conc = round(sum(m["importe"] for m in debitos_conc), 2)
    sum_cred_pend = round(sum(m["importe"] for m in creditos_pend), 2)
    sum_deb_pend = round(sum(m["importe"] for m in debitos_pend), 2)
    sum_hist = round(sum(m["importe"] for m in historicos), 2)

    saldo_conciliado = round(saldo_inicial + sum_cred_conc - sum_deb_conc, 2)
    saldo_final_esperado = round(
        saldo_conciliado + sum_cred_pend + sum_hist - sum_deb_pend, 2
    )

    return {
        "no_banco": no_banco,
        "desde": desde,
        "hasta": hasta,
        # Saldos
        "saldo_inicial": round(saldo_inicial, 2),
        "saldo_conciliado": saldo_conciliado,
        "saldo_final_esperado": saldo_final_esperado,
        # Listas
        "creditos_conc": creditos_conc,
        "debitos_conc": debitos_conc,
        "creditos_pend": creditos_pend,
        "debitos_pend": debitos_pend,
        "historicos": historicos,
        # Totales por sección (para el header de cada tabla)
        "sum_cred_conc": sum_cred_conc,
        "sum_deb_conc": sum_deb_conc,
        "sum_cred_pend": sum_cred_pend,
        "sum_deb_pend": sum_deb_pend,
        "sum_hist": sum_hist,
        # Combinado: creditos pendientes (PC) + historicos (banco no en PC)
        "sum_cred_pend_total": round(sum_cred_pend + sum_hist, 2),
    }
