"""Conversión de importe a letras en español — réplica de MODIFICA.PRG::LETRA/IMPCHEQ.

El dBase imprime el cheque físico con el monto en letras (procedimiento
`LETRA` + `IMPCHEQ`, MODIFICA.PRG:1266-1393). Este módulo reproduce esa
conversión para la pantalla imprimible `bancos.cheque_imprimible`.

Convenciones del legacy:
  - Centavos como "NN/100" (ej. "50/100"), igual que `CCC` en IMPCHEQ.
  - "uno" -> "un" antes de "mil"/"millón" (IIF(CIF='uno','un') del PRG).
  - "cien" exacto vs "ciento N".
  - Moneda: dólares (Ecuador). Texto final en MAYÚSCULAS como el cheque.

Función pura: no depende de la base ni de Flask. Fácil de testear.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_UNIDADES = [
    "", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
    "diez", "once", "doce", "trece", "catorce", "quince", "dieciseis",
    "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidos",
    "veintitres", "veinticuatro", "veinticinco", "veintiseis", "veintisiete",
    "veintiocho", "veintinueve",
]
_DECENAS = ["", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
_CENTENAS = [
    "", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos",
    "seiscientos", "setecientos", "ochocientos", "novecientos",
]


def _centena_a_letras(n: int) -> str:
    """0..999 -> letras."""
    if n == 0:
        return ""
    if n == 100:
        return "cien"
    centena, resto = divmod(n, 100)
    partes: list[str] = []
    if centena:
        partes.append(_CENTENAS[centena])
    if resto:
        if resto < 30:
            partes.append(_UNIDADES[resto])
        else:
            dec, uni = divmod(resto, 10)
            partes.append(f"{_DECENAS[dec]} y {_UNIDADES[uni]}" if uni else _DECENAS[dec])
    return " ".join(partes)


def _entero_a_letras(n: int) -> str:
    """Entero >= 0 -> letras (hasta miles de millones)."""
    if n == 0:
        return "cero"
    millones, resto = divmod(n, 1_000_000)
    miles, cientos = divmod(resto, 1_000)
    partes: list[str] = []
    if millones:
        partes.append("un millon" if millones == 1 else f"{_entero_a_letras(millones)} millones")
    if miles:
        if miles == 1:
            partes.append("mil")
        else:
            txt = _centena_a_letras(miles)
            if txt.endswith("uno"):
                txt = txt[:-3] + "un"
            partes.append(f"{txt} mil")
    if cientos:
        partes.append(_centena_a_letras(cientos))
    return " ".join(p for p in partes if p).strip()


def numero_a_letras(monto) -> dict:
    """Importe -> representación en letras para el cheque.

    Devuelve dict: letras (parte entera en palabras, MAYÚSCULAS), centavos
    ("NN/100"), texto (completo, "... NN/100 DOLARES"), en_numeros
    ("1.230,50", formato Ecuador). Réplica de IMPCHEQ.
    """
    d = Decimal(str(monto or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negativo = d < 0
    d = abs(d)
    entero = int(d)
    centavos = int((d - entero) * 100)

    letras = _entero_a_letras(entero).upper()
    cent_str = f"{centavos:02d}/100"
    texto = f"{letras} {cent_str} DOLARES"
    if negativo:
        texto = "MENOS " + texto

    en_numeros = f"{entero:,}".replace(",", ".") + f",{centavos:02d}"
    if negativo:
        en_numeros = "-" + en_numeros

    return {"letras": letras, "centavos": cent_str, "texto": texto, "en_numeros": en_numeros}
