"""Parser del campo CONCEPTO heredado del dBase legacy.

En el dBase original (BANCOS.PRG::CAJA / CHEQUERA / etc.), el campo
CONCEPTO funcionaba como un **código estructurado** que disparaba side
effects automáticos: una entrada de caja con concepto "PICH 0123" no
sólo registraba el egreso de caja, también insertaba la entrada
correspondiente en el banco Pichincha. Idem para retiros, compras a
proveedor, anticipos en dólares, etc.

Este módulo es la **traducción de ese protocolo** a Python puro:
toma un string y, dado el contexto (qué provs existen, etc.), devuelve
una descripción estructurada del side effect que dispararía.

NO toca la DB. NO inserta nada. Sólo parsea.

Las reglas vienen del PRG legacy y del feedback de TMT 2026-05-12:

  PICH<resto>     → transfer al banco Pichincha (concepto resto)
  INTER<resto>    → transfer al banco Internacional
  RR<XX>          → retiro al socio XX
  IN.<CT><resto>  → movimiento en cuenta de dólares CT (anticipo)
  IN <CT><resto>  → idem (tolerante: punto o espacio como separador) TMT 2026-05-17
  INHB<resto>     → caja HB / capital / retiro especial (variante INHB)
  <PR> <resto>    → compra al proveedor PR (2 letras + espacio)
                    sólo si PR está en la lista de provs válidos

Si nada matchea, devuelve {"tipo": "none"}.

La detección de PR es la más delicada: necesita la lista de provs reales
de scintela.proveedor para no levantar falsos positivos con palabras
comunes que empiezan con 2 letras (ALQUILER, SUELDOS, etc.).
"""

from __future__ import annotations

import re

# Prefijos reservados — ningún proveedor puede usar estos códigos.
_PREFIJOS_RESERVADOS = ("PICH", "INTER", "RR", "IN.", "INHB")


def parse_concepto(concepto: str, ctx: dict | None = None) -> dict:
    """Devuelve la descripción del side effect que dispara este concepto.

    Args:
        concepto: el string completo (libre).
        ctx: dict opcional con:
            - provs_validos: set[str] — códigos de 2 chars válidos en
              scintela.proveedor. Necesario para detectar compras.
            - bancos: dict[str, int] — {"PICHINCHA": 10, "INTERNAC": 32}
              para resolver no_banco. Si no se pasa, se devuelve sólo
              el nombre y el caller lo resuelve.

    Returns:
        Dict con campo `tipo` y campos extras según el tipo:
          {"tipo": "transfer_banco", "banco_nombre": "PICHINCHA",
           "no_banco": 10|None, "resto": "0123 reemp"}
          {"tipo": "transfer_banco", "banco_nombre": "INTERNACIONAL", ...}
          {"tipo": "retiro_socio", "socio": "TM", "resto": ""}
          {"tipo": "dolares", "cuenta": "WE", "resto": "1234"}
          {"tipo": "caja_inhb", "resto": "..."}
          {"tipo": "compra_proveedor", "prov": "PR", "resto": "factura X"}
          {"tipo": "none"}

        Siempre incluye `concepto_original` para auditoría.
    """
    ctx = ctx or {}
    raw = concepto or ""
    s = raw.strip()
    upper = s.upper()

    base = {"concepto_original": raw}

    if not s:
        return {"tipo": "none", **base}

    # ────────────────────────────────────────────────────────────────
    # PICH<resto> — transfer al banco Pichincha
    # ────────────────────────────────────────────────────────────────
    if upper.startswith("PICH"):
        resto = s[4:].strip()
        no_banco = (ctx.get("bancos") or {}).get("PICHINCHA")
        return {
            "tipo": "transfer_banco",
            "banco_nombre": "PICHINCHA",
            "no_banco": no_banco,
            "resto": resto,
            **base,
        }

    # ────────────────────────────────────────────────────────────────
    # INTER<resto> — transfer al banco Internacional
    # ────────────────────────────────────────────────────────────────
    if upper.startswith("INTER"):
        resto = s[5:].strip()
        bancos = ctx.get("bancos") or {}
        # Aceptar varios alias del nombre legacy
        no_banco = (
            bancos.get("INTERNACIONAL")
            or bancos.get("INTERNAC")
            or bancos.get("INTER")
        )
        return {
            "tipo": "transfer_banco",
            "banco_nombre": "INTERNACIONAL",
            "no_banco": no_banco,
            "resto": resto,
            **base,
        }

    # ────────────────────────────────────────────────────────────────
    # INHB<resto> — variante especial caja HB / capital / retiros.
    # Va antes del check de IN. para no comerlo el `IN`.
    # ────────────────────────────────────────────────────────────────
    if upper.startswith("INHB"):
        return {"tipo": "caja_inhb", "resto": s[4:].strip(), **base}

    # ────────────────────────────────────────────────────────────────
    # IN.<CT><resto> — dólares (anticipo). CT son 2 chars.
    # TMT 2026-05-17 (decisión Tamara): acepta tanto `IN.MP` con punto
    # como `IN MP` con espacio. El dBase original usa el punto pero la
    # dueña tipea cualquiera de los dos — relajamos para que sea robusto.
    # INHB ya matcheó antes (línea ~112), así que `IN HB` no colisiona.
    # ────────────────────────────────────────────────────────────────
    sep_idx = None
    if upper.startswith("IN.") and len(s) >= 5:
        sep_idx = 3  # IN.<CT> → cuenta arranca en pos 3
    elif upper.startswith("IN ") and len(s) >= 5:
        sep_idx = 3  # IN <CT> → idem
    if sep_idx is not None:
        cuenta = upper[sep_idx:sep_idx + 2]
        if cuenta.isalpha():
            resto = s[sep_idx + 2:].strip()
            return {
                "tipo": "dolares",
                "cuenta": cuenta,
                "resto": resto,
                **base,
            }

    # ────────────────────────────────────────────────────────────────
    # RR<XX> — retiro al socio XX. XX son 2 chars de letras.
    # Acepta "RRTM", "RR TM" (con espacio), "RR  TM" (varios spaces).
    # ────────────────────────────────────────────────────────────────
    if upper.startswith("RR"):
        rest_after_rr = s[2:].lstrip()
        if len(rest_after_rr) >= 2:
            socio = rest_after_rr[:2].upper()
            if socio.isalpha():
                resto = rest_after_rr[2:].strip()
                return {
                    "tipo": "retiro_socio",
                    "socio": socio,
                    "resto": resto,
                    **base,
                }

    # ────────────────────────────────────────────────────────────────
    # Gasto V1..V9 — match por keyword conocido (PINTURA, LUZ, AGUA,
    # SUELDOS, EEQ, EMAAP, etc.). VA ANTES de compra_proveedor para que
    # el caso típico "CC PINTURA" caiga acá como gasto V6 en vez de
    # pretender ser compra al proveedor CC (que existe pero vende
    # insumos, no materia prima). TMT 2026-05-15.
    # ────────────────────────────────────────────────────────────────
    try:
        from modules.gastos.queries import sugerir_categoria as _sug
        num_gasto = _sug(s)
    except Exception:
        num_gasto = None
    if num_gasto is not None:
        return {
            "tipo": "gasto",
            "num": int(num_gasto),
            "resto": s,
            **base,
        }

    # ────────────────────────────────────────────────────────────────
    # <PR> <resto> — compra a proveedor (2 letras + espacio + algo).
    # Sólo si PR está en la lista de proveedores válidos. Va DESPUÉS
    # del match de gasto para que los keywords ganen.
    # ────────────────────────────────────────────────────────────────
    if len(s) >= 3 and s[2] == " ":
        prov_candidato = upper[:2]
        provs = ctx.get("provs_validos") or set()
        # Asegurar que no es un prefijo reservado de los anteriores.
        if (prov_candidato.isalpha()
                and prov_candidato in provs
                and prov_candidato not in [p[:2] for p in _PREFIJOS_RESERVADOS]):
            resto = s[3:].strip()
            return {
                "tipo": "compra_proveedor",
                "prov": prov_candidato,
                "resto": resto,
                **base,
            }

    # ────────────────────────────────────────────────────────────────
    # Sin match — concepto libre, no dispara side effect.
    # ────────────────────────────────────────────────────────────────
    return {"tipo": "none", **base}


def descripcion_humana(parsed: dict) -> str:
    """Devuelve un string user-friendly de qué va a pasar. Para preview UI.

    Vacío si el tipo es 'none' (no hay side effect).
    """
    t = parsed.get("tipo")
    if t == "transfer_banco":
        return f"+ entrada en banco {parsed['banco_nombre']}"
    if t == "retiro_socio":
        return f"+ retiro al socio {parsed['socio']}"
    if t == "dolares":
        return f"+ movimiento en cuenta USD {parsed['cuenta']}"
    if t == "caja_inhb":
        return "+ caja HB (variante INHB)"
    if t == "compra_proveedor":
        return f"+ compra al proveedor {parsed['prov']}"
    return ""


# ────────────────────────────────────────────────────────────────────────
# Tests rápidos — se corren con `python concepto_parser.py`.
# ────────────────────────────────────────────────────────────────────────
def _test() -> None:
    """Tests inline. Si algo falla, lanza AssertionError."""
    provs = {"PR", "AQ", "HY", "QI", "TT", "MH"}
    bancos = {"PICHINCHA": 10, "INTERNACIONAL": 32}
    ctx = {"provs_validos": provs, "bancos": bancos}

    cases = [
        # (concepto, ctx, expected_tipo, expected_extras)
        ("PICH 0123 reemp", ctx, "transfer_banco",
         {"banco_nombre": "PICHINCHA", "no_banco": 10, "resto": "0123 reemp"}),
        ("PICH", ctx, "transfer_banco",
         {"banco_nombre": "PICHINCHA", "no_banco": 10, "resto": ""}),
        ("INTER 999", ctx, "transfer_banco",
         {"banco_nombre": "INTERNACIONAL", "no_banco": 32, "resto": "999"}),
        ("RR TM", ctx, "retiro_socio", {"socio": "TM"}),
        ("RR12", ctx, "none", {}),  # 12 no es letras
        ("IN.WE 1234", ctx, "dolares", {"cuenta": "WE", "resto": "1234"}),
        ("INHB pago x", ctx, "caja_inhb", {"resto": "pago x"}),
        # INHB debe matchear antes que IN. (más específico).
        ("PR 123 pago", ctx, "compra_proveedor",
         {"prov": "PR", "resto": "123 pago"}),
        # TMT 2026-05-15: con la detección de gasto V1..V9 por keyword,
        # ALQUILER ahora matchea como tipo "gasto" (no como "none"). Antes
        # caía a "none" porque no es <PROV> + espacio.
        ("ALQUILER", ctx, "gasto", {"resto": "ALQUILER"}),
        ("XX 999", ctx, "none", {}),  # XX no está en provs_validos
        ("", ctx, "none", {}),
        ("  ", ctx, "none", {}),
        ("AQ pago factura 234", ctx, "compra_proveedor",
         {"prov": "AQ", "resto": "pago factura 234"}),
        # Sin ctx, las compras no se detectan (falta provs_validos).
        ("PR 123", None, "none", {}),
    ]

    for concepto, c, expected_tipo, expected_extras in cases:
        result = parse_concepto(concepto, c)
        assert result["tipo"] == expected_tipo, (
            f"concepto={concepto!r} → esperado {expected_tipo!r}, "
            f"obtuvo {result!r}"
        )
        for k, v in expected_extras.items():
            assert result.get(k) == v, (
                f"concepto={concepto!r} → campo {k!r} "
                f"esperado {v!r}, obtuvo {result.get(k)!r}"
            )

    # Test de descripcion_humana
    assert descripcion_humana(parse_concepto("PICH 0123", ctx)) == (
        "+ entrada en banco PICHINCHA"
    )
    assert descripcion_humana(parse_concepto("ALQUILER", ctx)) == ""

    print(f"OK — {len(cases)} casos pasaron.")


# ───────────────────────────────────────────────────────────────────────────
# Nota de importación Asinfo → código de compra/anticipo del programa
# ───────────────────────────────────────────────────────────────────────────
# En Asinfo, la `Nota` (factura_proveedor.descripcion) de cada importación
# lleva al final el código con que el PROGRAMA registró esa compra/anticipo:
# 2-3 letras (= scintela.proveedor.codigo_prov: AC=Ariescope, MH=More Human,
# AI=Aartimpex, …) + número (= scintela.compra.numero), casi siempre entre
# paréntesis. Ejemplos reales (con todo su desorden):
#     "ACMT/EXP/2026-27/8197 ( AC 36)"      → AC / 36
#     "INV 2026030405 ( MH  63)"            → MH / 63   (doble espacio)
#     "ACMT/EXP/2026-27/8157 ( AC 22"       → AC / 22   (sin cerrar paréntesis)
#     "ACMT/EXP/2026-27/8044 ( AC 25)\n"    → AC / 25   (basura al final)
#     "AYF2653 ( AI 15 )    ----1"          → AI / 15   (split de una factura)
#     "ALG ( MH 64-65 )"                    → MH / 64 (numero_hasta=65, rango)
#
# OJO con falsos positivos: "INV 2026030405" también parece "letras+número".
# Por eso (a) preferimos el código entre paréntesis, (b) tomamos la ÚLTIMA
# coincidencia (el código va al final), (c) el código del programa son grupos
# de 2-3 letras seguidos de número chico.

#: paréntesis abierto + 2-3 letras + número (+ rango opcional). Lo preferido.
_NOTA_CODE_PAREN = re.compile(
    r"\(\s*([A-Za-z]{2,3})\s+0*(\d+)(?:\s*[-/]\s*0*(\d+))?", re.UNICODE
)
#: fallback — 2-3 letras + número cerca del final (sin exigir paréntesis).
_NOTA_CODE_BARE = re.compile(
    r"([A-Za-z]{2,3})\s+0*(\d+)(?:\s*[-/]\s*0*(\d+))?\s*\)?\s*[\s\W]*$", re.UNICODE
)


def parse_nota_importacion(nota: str | None) -> dict:
    """Extrae el código de compra/anticipo del programa embebido en la Nota
    de una importación de Asinfo.

    Returns:
        {"prov": "AC", "numero": 36, "numero_hasta": None,
         "codigo": "AC 36", "raw": "<nota original>"}  si matchea,
        {} si no encuentra un código.
    """
    raw = nota or ""
    s = raw.strip()
    if not s:
        return {}

    # 1) Preferimos códigos entre paréntesis; nos quedamos con el último.
    matches = list(_NOTA_CODE_PAREN.finditer(s))
    m = matches[-1] if matches else _NOTA_CODE_BARE.search(s)
    if not m:
        return {}

    prov = m.group(1).upper()
    numero = int(m.group(2))
    numero_hasta = int(m.group(3)) if m.lastindex and m.group(3) else None
    codigo = f"{prov} {numero}" + (f"-{numero_hasta}" if numero_hasta else "")
    return {
        "prov": prov,
        "numero": numero,
        "numero_hasta": numero_hasta,
        "codigo": codigo,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# AÑO / CAMPAÑA de las importaciones — TMT 2026-07-29 (dueña).
#
# El número de la importación ("AC 58") se REUSA cada campaña: en las últimas
# 400 importaciones de Asinfo hay 78 códigos repetidos sobre 274 (el "AC 58"
# aparece 3 veces). Hasta hoy el desempate era "la importación de fecha más
# cercana dentro de 300 días", y cuando la del año correcto todavía no existe
# en Asinfo el anticipo se pegaba a la del año anterior: el AC 77 del 18/06/26
# terminó contra IM-0000495 (campaña 2025-26) y el AC 58 del 21/04/26 contra
# IM-0000527 en vez de IM-0000622 — mientras sus hermanos 57 y 59, cargados el
# MISMO día, sí cayeron bien.
#
# Solución (dueña): el anticipo lleva el año escrito en el CONCEPTO con formato
# `58/26`, y el match exige año igual. El año viaja en el concepto para que
# sobreviva al sync del dBase (DOLARES.DBF) y quede igual en los dos programas.
#
# Del lado de la importación el año sale de la CAMPAÑA de la nota
# ("ACMT/EXP/2026-27/8368" → campaña que ARRANCA en 2026) y, si la nota no la
# trae (MH/AI/KX no la escriben), del año de la fecha de la factura.
# ---------------------------------------------------------------------------

#: nº + barra + año en el concepto de un anticipo: "58/26", "58 / 2026".
#: El (?<!\d)/(?!\d) evita cortar números largos por la mitad.
_REF_ANIO_ANTICIPO = re.compile(r"(?<!\d)0*(\d{1,6})\s*/\s*(\d{4}|\d{2})(?!\d)")
#: primer número suelto del concepto ("31 SALDO" → 31, "AC 95" → 95).
_REF_SUELTA = re.compile(r"\b0*(\d{1,6})\b")
#: campaña dentro de la nota de Asinfo, SIEMPRE entre barras:
#: "ACMT/EXP/2026-27/8368". Las barras son las que evitan el falso positivo
#: de "INV HY336-26-1" (que NO es una campaña).
_CAMPANA_NOTA = re.compile(r"/\s*\d{3,4}\s*-\s*(\d{2})\s*/")


def _anio_a_4(valor: int) -> int:
    """26 → 2026 · 2026 → 2026. Los años de 2 dígitos son siempre 20xx."""
    v = int(valor)
    return v if v >= 1000 else 2000 + v


def parse_ref_anticipo(concepto: str | None) -> dict:
    """Número de importación + año explícito del concepto de un anticipo.

        "58/26 SALDO" → {"numero": 58, "anio": 2026}
        "31 SALDO"    → {"numero": 31, "anio": None}   (como siempre)
        "AC 95"       → {"numero": 95, "anio": None}

    El número sale igual que antes (primer número suelto) para no romper los
    anticipos vivos que todavía no tienen año.
    """
    s = (concepto or "").strip()
    if not s:
        return {"numero": None, "anio": None}
    m = _REF_ANIO_ANTICIPO.search(s)
    if m:
        return {"numero": int(m.group(1)), "anio": _anio_a_4(int(m.group(2)))}
    m2 = _REF_SUELTA.search(s)
    return {"numero": int(m2.group(1)) if m2 else None, "anio": None}


def anio_importacion(nota: str | None, fecha: str | None = None) -> dict:
    """Año de una importación de Asinfo.

    1. Si la nota trae la CAMPAÑA entre barras ("ACMT/EXP/2026-27/8368"), el
       año es el de INICIO de esa campaña — se deduce del cierre (27 → 2026),
       que es el dígito que los proveedores escriben bien incluso cuando el
       primero viene con typo ("205-26", "202-27", "206-27" están todos en la
       base real).
    2. Si no, el año de la fecha de la factura del proveedor.

    Devuelve {"anio": int | None, "de_campana": bool}. `de_campana` marca de
    dónde salió: entre dos candidatas del mismo año, la que tiene la campaña
    escrita gana (es el dato del proveedor, no una inferencia por fecha).
    """
    m = _CAMPANA_NOTA.search(nota or "")
    if m:
        cierre = int(m.group(1))
        if 1 <= cierre <= 99:
            return {"anio": _anio_a_4(cierre) - 1, "de_campana": True}
    try:
        return {"anio": int(str(fecha)[:4]), "de_campana": False}
    except (TypeError, ValueError):
        return {"anio": None, "de_campana": False}


if __name__ == "__main__":
    _test()
