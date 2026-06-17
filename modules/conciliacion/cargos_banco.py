"""Clasificar pendientes de banco en PENDIENTES REALES vs CARGOS DEL BANCO.

TMT 2026-06-04 dueña: 'si lo que hizo Alex está bien, que cuando imprimamos la
hoja la nuestra sea igual'. Alex separa los cargos del banco (comisiones, IVA,
costos de cheque, y el neto del par PAGO SENAE/acreditación de aduana) y los
deja abajo como DIFERENCIA, en vez de mezclarlos con los pendientes reales.

Un PENDIENTE REAL (depósito en tránsito, cheque girado sin cobrar) en algún
momento cruza contra el programa. Un CARGO DEL BANCO (fee) NUNCA cruza — hay
que asentarlo como gasto. Mezclarlos hace que la diferencia se anule a 0 y se
esconda. Acá los separamos con una regla CONSERVADORA: solo van a "cargos" los
conceptos que nunca son depósitos/cheques en tránsito, más el par de aduana
que casi se cancela. Todo lo demás queda como pendiente real.

Validado 2026-06-04 contra la conciliación de Alex: 150 pendientes →
146 reales (neto 164.247,95) + 4 cargos (neto −99,84).
"""
from __future__ import annotations

import re

# Conceptos que son SIEMPRE cargos del banco (nunca depósito/cheque en tránsito).
# OJO: "COST CHEQUE DEVUELTO" es fee; "CHEQUE DEVUELTO" (sin COST) es un evento
# real → NO se clasifica como cargo.
# TMT decisión 2026-06-17: ampliar regex para capturar conceptos del Pichincha
# que la dueña (Tamara) reportó como "gastos y comisiones del banco" que no
# aparecían en el xlsx de pendientes. ANTES solo COMISION/IVA/COST CHEQUE/
# GASTO/MANTEN entraban. AHORA también:
#   ISD-PAG / ISD : Impuesto Salida Divisas (cargo del banco al pagar al exterior)
#   IMPUESTO     : cualquier impuesto explícito (anti-ruido: palabra completa)
#   INTER[EÉ]S(ES) : intereses cobrados (no devengados)
#   COSTO TRANSFER : costo de transferencia
#   COBRO SERVICIO : cobro de servicio bancario
#   DEBITO AUTOMA(TICO) : débito automático
# Conservador: usa límites de palabra (\b) para no matchear sustrings ruidosos.
_RE_FEE = re.compile(
    r"COMISI[OÓ]N|\bIVA\b|COST(?:O)?\s+CHEQUE|GASTO|MANTEN|"
    r"\bISD[-\s]?PAG\b|\bISD\b|"
    r"\bIMPUESTO\b|"
    r"\bINTER[EÉ]S(?:ES)?\b|"
    r"COSTO\s+TRANSFER|"
    r"COBRO\s+SERVICIO|"
    r"D[EÉ]BITO\s+AUTOM",
    re.I,
)
_RE_SENAE = re.compile(r"PAGO\s+SENAE", re.I)


def _es_acreditacion(documento: str | None, concepto: str | None) -> bool:
    """Acreditación de aduana (la pata + del par SENAE). Ej: doc 'AC97',
    o concepto que mencione 'AC NN CAE' / 'ACREDITA'."""
    d = (documento or "").strip().upper()
    c = (concepto or "").upper()
    return bool(re.match(r"^AC\s*\d", d)) or "ACREDITA" in c or bool(re.search(r"\bAC\s*\d+\s*CAE", c))


def es_fee(concepto: str | None) -> bool:
    return bool(_RE_FEE.search(concepto or ""))


def clasificar_cargos(items: list[dict], par_tol: float = 1000.0, par_pct: float = 0.05):
    """Divide items en (reales, cargos).

    items: list de dict con al menos {documento, concepto, monto}, donde
           `monto` viene SIGNADO (+ crédito, − débito).

    Regla:
      1) Fees por concepto (comisión, IVA, costo de cheque, gasto) → cargo.
      2) Par de aduana: un PAGO SENAE (débito) + una acreditación AC (crédito)
         que casi se cancelan (|dif| < max(par_tol, par_pct·monto)) → ambos cargo
         (su neto es la comisión). Un PAGO SENAE suelto (sin acreditación que lo
         empareje) queda como pendiente real — cruza después con su AC del PC.
    """
    cargos_idx: set[int] = set()
    for i, it in enumerate(items):
        if es_fee(it.get("concepto")):
            cargos_idx.add(i)

    senae = [
        i for i, it in enumerate(items)
        if i not in cargos_idx and _RE_SENAE.search(it.get("concepto") or "")
        and float(it.get("monto") or 0) < 0
    ]
    acred = [
        i for i, it in enumerate(items)
        if i not in cargos_idx and _es_acreditacion(it.get("documento"), it.get("concepto"))
        and float(it.get("monto") or 0) > 0
    ]
    usados: set[int] = set()
    for si in senae:
        x = abs(float(items[si]["monto"]))
        best, bestd = None, 1e18
        for ai in acred:
            if ai in usados:
                continue
            d = abs(x - float(items[ai]["monto"]))
            if d < bestd:
                bestd, best = d, ai
        if best is not None and bestd < max(par_tol, par_pct * x):
            cargos_idx.add(si)
            cargos_idx.add(best)
            usados.add(best)

    reales = [it for i, it in enumerate(items) if i not in cargos_idx]
    cargos = [it for i, it in enumerate(items) if i in cargos_idx]
    return reales, cargos


def _split(items: list[dict]) -> dict:
    cred = round(sum(float(x["monto"]) for x in items if float(x["monto"]) > 0), 2)
    deb = round(sum(-float(x["monto"]) for x in items if float(x["monto"]) < 0), 2)
    return {"creditos": cred, "debitos": deb, "neto": round(cred - deb, 2), "n": len(items)}


def resumen(items: list[dict]) -> dict:
    """{reales:{creditos,debitos,neto,n}, cargos:{...}, rows_reales, rows_cargos}."""
    reales, cargos = clasificar_cargos(items)
    return {
        "reales": _split(reales),
        "cargos": _split(cargos),
        "rows_reales": reales,
        "rows_cargos": cargos,
    }
