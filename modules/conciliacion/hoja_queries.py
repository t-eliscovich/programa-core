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
        COALESCE(c.nombre, p.nombre, '') AS contraparte
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


def _cheques_de_transaccion(id_transaccion: int) -> list[dict[str, Any]]:
    """Cheques individuales que componen un depósito agrupado.

    TMT 2026-05-28 dueña: 'dejar de agrupar los cheques, cada cheque una linea'.
    En PC un 'dep.N ch' es UNA fila en transacciones_bancarias pero los N
    cheques individuales viven en scintela.cheque (vinculados por
    chequextransaccion). Devolvemos cada uno como su propia fila.
    """
    rows = _db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.importe, c.codigo_cli, c.banco,
               COALESCE(cli.nombre, '') AS cliente_nombre
          FROM scintela.chequextransaccion cxt
          JOIN scintela.cheque c ON c.id_cheque = cxt.id_cheque
          LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
         WHERE cxt.id_transaccion = %(id_t)s
         ORDER BY c.importe DESC, c.id_cheque
        """,
        {"id_t": id_transaccion},
    ) or []
    return rows


def _detectar_agrupado_simple(concepto: str) -> int:
    """N >= 2 si 'dep.25 ch' / '25 ch.', 0 si no. Heurística mínima.

    No queremos depender de matcher_banco aquí (evitamos import circular).
    """
    if not concepto:
        return 0
    import re
    m = re.search(r"(\d{1,4})\s*ch\.?", concepto.lower())
    if not m:
        return 0
    n = int(m.group(1))
    return n if n >= 2 else 0


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

    out: list[dict[str, Any]] = []
    for r in rows:
        concepto = (r.get("concepto") or "").strip()
        documento = (r.get("documento") or "").strip()
        id_t = r.get("id_transaccion")
        # Solo intentamos explotar agrupados en DEPÓSITOS (documento='DE')
        # — para DOCS_DEBITO cada cheque emitido ya es su propia fila.
        n_ch = _detectar_agrupado_simple(concepto) if documento.upper() == "DE" else 0
        if n_ch >= 2 and id_t is not None:
            cheques = _cheques_de_transaccion(int(id_t))
            if cheques:
                # Expandimos: cada cheque como su propia línea.
                # NB: no_cheque y codigo_cli pueden ser int/None — siempre cast a str.
                for c in cheques:
                    no_ch = c.get("no_cheque")
                    banco_txt = str(c.get("banco") or "").strip()
                    out.append({
                        "id_transaccion": id_t,
                        "id_cheque": c.get("id_cheque"),
                        "fecha": r.get("fecha"),
                        "documento": "CH",  # mostramos como cheque individual
                        "concepto": "Cheque " + (str(no_ch) if no_ch else "—")
                                     + (f" · {banco_txt}" if banco_txt else ""),
                        "numero": str(no_ch).strip() if no_ch else "",
                        "importe": _fmt(c.get("importe")),
                        "prov": str(c.get("codigo_cli") or "").strip(),
                        "contraparte": str(c.get("cliente_nombre") or "").strip(),
                        "stat": str(r.get("stat") or "").strip(),
                        "es_cheque_de_deposito": True,
                        "deposito_concepto": concepto,
                    })
                continue
            # Si no encontramos los cheques individuales, dejamos el agrupado.
        out.append({
            "id_transaccion": id_t,
            "fecha": r.get("fecha"),
            "documento": documento,
            "concepto": concepto,
            "numero": (r.get("numreferencia") or "").strip(),
            "importe": _fmt(r.get("importe")),
            "prov": (r.get("prov") or "").strip(),
            "contraparte": (r.get("contraparte") or "").strip(),
            "stat": (r.get("stat") or "").strip(),
            "es_cheque_de_deposito": False,
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
