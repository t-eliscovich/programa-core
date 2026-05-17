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


if __name__ == "__main__":
    _test()
