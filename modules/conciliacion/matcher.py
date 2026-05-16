"""Matcher: compara líneas del estado de cuenta vs cheques depositados.

Reglas:
    - Los cheques buscados están en stat='D' (depositado) con `fechad <= hoy`.
    - Un match requiere:
        (a) la referencia del banco matchea `no_cheque`, O
        (b) el concepto contiene el `no_cheque`,
        Y
        (c) el débito o crédito del banco matchea el importe (± 0.01 por rounding).
    - Un cheque con >N días depositado sin match se flagea como "sospecha de
      rechazo". Default N=3 días hábiles.

Output:
    MatchResult(matches=[...], sospechosos=[...]) donde cada uno es un dict con
    la info necesaria para que la UI muestre la bandeja.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from modules.conciliacion.parser import BancoLinea


@dataclass
class MatchResult:
    matches: list[dict] = field(default_factory=list)
    sospechosos: list[dict] = field(default_factory=list)


def _monto_matches(importe_cheque: float, debito: Decimal, credito: Decimal) -> bool:
    """Cheque rebotado aparece como DÉBITO; depósito como CRÉDITO.

    Los bancos asientan el "cheque devuelto" como un débito del mismo
    importe. El movimiento original (ingreso al depositar) está como
    crédito. Matcheamos contra AMBOS para ser tolerantes al estado de
    cuenta: puede que sólo esté el débito del rebote, o también el crédito
    original si el período cubre ambos.
    """
    tol = Decimal("0.01")
    imp = Decimal(str(importe_cheque))
    return abs(debito - imp) <= tol or abs(credito - imp) <= tol


def _referencia_matches(no_cheque: str, linea: BancoLinea) -> bool:
    no = (no_cheque or "").strip().lstrip("0")
    if not no:
        return False
    ref = (linea.referencia or "").strip().lstrip("0")
    if ref == no:
        return True
    # buscar el número dentro del concepto (" CHEQUE 1234 DEVUELTO ")
    concepto = (linea.concepto or "").upper()
    return no in concepto or no in (linea.referencia or "")


def matchear(
    lineas_banco: list[BancoLinea],
    cheques_depositados: list[dict],
    dias_sospecha: int = 3,
    hoy: date | None = None,
) -> MatchResult:
    """Matcha cheques vs líneas del banco.

    `cheques_depositados` es una lista de dicts con al menos:
        id_cheque, no_cheque, importe, fechad, codigo_cli

    Los cheques con `fechad` antigua y sin match en el estado de cuenta se
    incluyen en `sospechosos` — candidatos a marcar como rechazados.
    """
    hoy = hoy or date.today()
    result = MatchResult()

    # Índice defensivo: si tenemos miles de líneas, este paso es O(N*M).
    # Para el uso típico (un mes de cheques vs un mes de estado), es trivial.
    for cheque in cheques_depositados:
        no = str(cheque.get("no_cheque", ""))
        importe = float(cheque.get("importe") or 0)
        matched_line: BancoLinea | None = None
        for linea in lineas_banco:
            if _referencia_matches(no, linea) and _monto_matches(importe, linea.debito, linea.credito):
                matched_line = linea
                break

        if matched_line is not None:
            result.matches.append({
                "id_cheque": cheque.get("id_cheque"),
                "no_cheque": no,
                "importe": importe,
                "codigo_cli": cheque.get("codigo_cli"),
                "fecha_banco": matched_line.fecha,
                "concepto_banco": matched_line.concepto,
                "es_debito": matched_line.debito > 0,  # rebote si True
            })
        else:
            fechad = cheque.get("fechad")
            # Cheque sin match que ya "debería" haber compensado hace rato.
            if isinstance(fechad, date):
                dias = (hoy - fechad).days
                if dias >= dias_sospecha:
                    result.sospechosos.append({
                        "id_cheque": cheque.get("id_cheque"),
                        "no_cheque": no,
                        "importe": importe,
                        "codigo_cli": cheque.get("codigo_cli"),
                        "fechad": fechad,
                        "dias_sin_match": dias,
                        "razon": (
                            f"Cheque depositado hace {dias} días sin movimiento "
                            "correspondiente en el estado de cuenta."
                        ),
                    })

    return result
