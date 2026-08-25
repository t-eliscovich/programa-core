"""El match PC ↔ Asinfo de una factura: de qué TIPO es cada documento.

Esto vivía inline en `facturas/views.py` y por eso el tipo (F · Factura,
D · Devolución, N · NTEN, NC · NC Financiera, NCNT) sólo existía mientras se
pintaba la pantalla de Cartera: en la pestaña Estado ni se calculaba, así que
filtrar por tipo ahí daba SIEMPRE cero, y los totales del encabezado —que
salen del SQL— nunca lo veían. TMT 2026-08-14 (dueña): *"no anda el filtro"*
y *"¿cuánto suman?"*, dos síntomas del mismo agujero.

Sacarlo acá permite correrlo FUERA de la pantalla (backfill + repaso diario) y
guardar el resultado en `factura.asinfo_tipo`, que es lo que deja filtrar y
sumar en SQL.

⚠️ El tipo NO es un dato que Asinfo entregue por factura: es el resultado de un
match difuso. En orden: número completo → sufijo numérico → (cliente, fecha,
kg) con desempate por dólares → heurística de signo invertido. Todo el
comportamiento se movió TAL CUAL; si algo cambia acá, cambia la clasificación
de 30.000 facturas.
"""
from __future__ import annotations

from datetime import date

# Asinfo sólo tiene data limpia desde acá: lo anterior no va a matchear nunca
# y no es un error (la cartera legacy es 2021-2024).
ASINFO_DESDE_EFECTIVO = date(2025, 1, 1)

TIPOS_POSITIVOS = ("FACTURA", "NTEN", "NC_FINANCIERA")
TIPOS_NEGATIVOS = ("DEVOLUCION", "NCNT")

# Los tipos que la pantalla ofrece en el combo, con la letra que muestra.
TIPOS_UI = {
    "F": "FACTURA",
    "D": "DEVOLUCION",
    "N": "NTEN",
    "NC": "NC_FINANCIERA",
    "NCNT": "NCNT",
}


def inicializar(filas) -> None:
    """Deja las claves `asinfo_*` en None/False para que el template no rompa
    aunque el puente esté caído o la vista no pida el enriquecimiento."""
    for f in filas:
        f["asinfo_kg"] = None
        f["asinfo_usd"] = None
        f["asinfo_diff_kg"] = None
        f["asinfo_diff_usd"] = None
        f["asinfo_tipo"] = None
        f["asinfo_signo_invertido"] = False
        f["asinfo_pre_cutoff"] = bool(
            f.get("fecha") and f["fecha"] < ASINFO_DESDE_EFECTIVO)


def clasificar(filas, asinfo_rows) -> None:
    """Anota `asinfo_tipo`/`asinfo_kg`/`asinfo_usd`/… en cada fila, in-place.

    `filas`: dicts de `scintela.factura` (numf, numf_completo, fecha, kg,
    importe, codigo_cli). `asinfo_rows`: lo que devuelve
    `asinfo.service.facturas_periodo`.
    """
    _TIPOS_POSITIVOS = TIPOS_POSITIVOS
    _TIPOS_NEGATIVOS = TIPOS_NEGATIVOS
    # TMT 2026-05-22 — extendido: muchos clientes (BED, EDU, BAN…)
    # facturan via NTEN (nota de entrega) en lugar de FACTURA común.
    # Hasta ahora el matcher solo veía FACTURA/DEVOLUCION y dejaba
    # cientos de facturas PC con kg>0 sin match.
    #   - FACTURA + NTEN + NC_FINANCIERA  → contra PC kg > 0
    #     (NTEN tiene kg positivos como FACTURA. NC_FINANCIERA va
    #     acá también para que kg=0 pueda matchearlas si tienen
    #     número completo coincidente.)
    #   - DEVOLUCION + NCNT              → contra PC kg < 0
    #
    # Indexamos por DOS claves dentro de cada universo:
    #   1) `numero` completo ("001-099-000010588" o "NTEN-10309") → match directo
    #   2) sufijo numérico (int 10588 / 10309) → contra el numf chico de PC
    idx_factura_completo: dict[str, dict] = {}
    idx_factura_numf: dict[int, dict] = {}
    idx_devolucion_completo: dict[str, dict] = {}
    idx_devolucion_numf: dict[int, dict] = {}
    _TIPOS_POSITIVOS = ("FACTURA", "NTEN", "NC_FINANCIERA")
    _TIPOS_NEGATIVOS = ("DEVOLUCION", "NCNT")
    for r in asinfo_rows:
        tipo = r.get("tipo")
        numero = r.get("numero")
        if not numero:
            continue
        if tipo in _TIPOS_POSITIVOS:
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif tipo in _TIPOS_NEGATIVOS:
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            continue
        # No pisar si ya hay match con FACTURA (más confiable que NTEN).
        if numero not in c_idx:
            c_idx[numero] = r
        sufijo = numero.split("-")[-1] if "-" in numero else numero
        try:
            sufijo_int = int(sufijo)
            if sufijo_int not in n_idx:
                n_idx[sufijo_int] = r
        except (ValueError, TypeError) as _e:
            from modules._lib.silencios import avisar
            avisar(__name__, "_parse_num", _e, nivel="debug")
    # TMT 2026-05-22 — índice por (cliente, fecha, kg redondeado).
    # Muchas filas PC tienen numf=0 (sin número Asinfo cargado) y
    # el match por número no funciona. Pero los importes USD coinciden
    # exactamente con la card 199 (que ya viene sin IVA). Hacemos
    # un índice compuesto para el fallback heurístico.
    from collections import defaultdict as _dd
    idx_compuesto: dict[tuple, list[dict]] = _dd(list)
    # TMT 2026-05-22 — índice ABS (sin signo). Para detectar
    # misregistros: PC cargada como devolución (kg<0) cuando en
    # Asinfo es FACTURA con kg>0 (mismo |kg|, mismo |usd|).
    idx_compuesto_abs: dict[tuple, list[dict]] = _dd(list)
    for r in asinfo_rows:
        tipo = r.get("tipo")
        if tipo not in (_TIPOS_POSITIVOS + _TIPOS_NEGATIVOS):
            continue
        cli = (r.get("cliente_codigo") or "").strip().upper()
        fecha_ai = r.get("fecha")
        kg_ai = float(r.get("kg") or 0)
        if not (cli and fecha_ai):
            continue
        # Redondeamos kg a 2 decimales para tolerar drift mínimo de
        # formato. usd queda en la fila para validación posterior.
        key = (cli, str(fecha_ai)[:10], round(kg_ai, 2))
        idx_compuesto[key].append(r)
        key_abs = (cli, str(fecha_ai)[:10], round(abs(kg_ai), 2))
        idx_compuesto_abs[key_abs].append(r)

    # Mergear: elegir índice según signo del kg de PC.
    #   kg > 0  → buscar en FACTURA+NTEN+NC_FINANCIERA
    #   kg < 0  → buscar en DEVOLUCION+NCNT
    #   kg == 0 → no matchear (NC financiera, ajustes)
    for f in filas:
        pc_kg = float(f.get("kg") or 0)
        # TMT 2026-05-26 — facturas MARCADAS (#DUP, #SIN_ASINFO, etc.)
        # se excluyen de match Asinfo: representan filas explícitamente
        # marcadas por humano/script como "no requiere match" o "dup
        # conocido". El prefijo '#' nunca aparece en numeros Asinfo
        # reales (que son '001-099-...' / 'NTEN-...' / 'NCNT-...').
        if (f.get("numf_completo") or "").startswith("#"):
            f["asinfo_marcada"] = f["numf_completo"]
            f["asinfo_tipo"] = "MARCADA"
            continue
        # TMT 2026-05-22 — antes kg=0 se saltaba. Ahora también
        # intentamos matchear NC financieras (kg=0, importe negativo)
        # por el universo "positivo" (que ya incluye NC_FINANCIERA).
        if pc_kg > 0:
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif pc_kg < 0:
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            # kg=0 → intentar contra ambos índices, prefiriendo el negativo
            # si el importe PC es negativo.
            pc_imp_signo = float(f.get("importe") or 0)
            if pc_imp_signo < 0:
                c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
            else:
                c_idx, n_idx = idx_factura_completo, idx_factura_numf
        r_ai = None
        numero = (f.get("numf_completo") or "").strip()
        if numero:
            r_ai = c_idx.get(numero)
        if r_ai is None and f.get("numf"):
            try:
                r_ai = n_idx.get(int(f["numf"]))
            except (ValueError, TypeError) as _e:
                from modules._lib.silencios import avisar
                avisar(__name__, "_parse_num", _e, nivel="debug")
        # TMT 2026-05-22 — Fallback heurístico para PC sin numf:
        # match por (codigo_cli + fecha + kg exacto) y validar
        # que los importes coincidan.
        #
        # TMT 2026-05-29 dueña: 'asinfo siempre tiene numero, PC no'.
        # Ampliado con 3 estrategias en cascada:
        #   (a) USD exacto: |pc - ai| < 0.5  (PC sin IVA = card 199)
        #   (b) USD con IVA 12%: |pc - ai * 1.12| < 0.5
        #   (c) Tolerancia 15% sobre el importe: cubre IVA + redondeos
        # Si UNA sola candidata cuadra en CUALQUIERA de las 3, match.
        if r_ai is None:
            cli_pc = (f.get("codigo_cli") or "").strip().upper()
            fecha_pc = f.get("fecha")
            if cli_pc and fecha_pc:
                key = (cli_pc, str(fecha_pc)[:10], round(pc_kg, 2))
                candidatos = idx_compuesto.get(key, [])
                pc_imp = float(f.get("importe") or 0)

                def _coincide_usd(ai_usd: float) -> bool:
                    # Estrategia (a) USD exacto.
                    if abs(ai_usd - pc_imp) < 0.5:
                        return True
                    # Estrategia (b) PC trae IVA 12%, Asinfo no.
                    if abs(pc_imp - ai_usd * 1.12) < 0.5:
                        return True
                    # Estrategia (c) tolerancia 15% (cubre IVA 12-14%
                    # + redondeos + ajustes chicos). Solo si el monto
                    # no es trivial (>= $1) para evitar matchear
                    # comisiones de centavos al voleo.
                    base = max(abs(ai_usd), abs(pc_imp), 1.0)
                    if base >= 1.0 and abs(ai_usd - pc_imp) / base < 0.15:
                        return True
                    return False

                ok = [c for c in candidatos
                      if _coincide_usd(float(c.get("usd") or 0))]
                if len(ok) == 1:
                    r_ai = ok[0]
                elif len(ok) > 1:
                    # TMT 2026-05-28 — dueña: muchas operaciones tienen
                    # FACTURA + NTEN simultáneas con mismo cli/kg/usd
                    # (la NTEN es nota de entrega, la FACTURA es el
                    # comprobante fiscal). Preferimos FACTURA > NTEN.
                    facts = [c for c in ok if c.get("tipo") == "FACTURA"]
                    ntens = [c for c in ok if c.get("tipo") == "NTEN"]
                    if len(facts) == 1:
                        r_ai = facts[0]
                    elif len(ntens) == 1:
                        r_ai = ntens[0]
                    # Si hay >1 FACTURA o >1 NTEN, sigue siendo ambiguo
                    # (dejar huérfana — requiere análisis manual).
        # TMT 2026-05-22 — Detección de signo invertido. Cuando PC
        # tiene kg<0 (cargada como devolución) y Asinfo tiene una
        # FACTURA/NTEN positiva con mismo |kg| y mismo |usd|,
        # asumimos que PC se cargó con signo invertido por error.
        # Match con tolerancia más amplia porque el USD de PC
        # podría tener IVA (la card 199 lo trae sin IVA).
        signo_invertido = False
        if r_ai is None and pc_kg < 0:
            cli_pc = (f.get("codigo_cli") or "").strip().upper()
            fecha_pc = f.get("fecha")
            if cli_pc and fecha_pc:
                key_abs = (cli_pc, str(fecha_pc)[:10], round(abs(pc_kg), 2))
                candidatos = idx_compuesto_abs.get(key_abs, [])
                pc_imp_abs = abs(float(f.get("importe") or 0))
                # Tolerancia 15% para absorber IVA (USA: típico 12-14%
                # ya neteado o no). Filtramos a tipos POSITIVOS y
                # validamos que el kg sea positivo en Asinfo.
                ok = []
                for c in candidatos:
                    if c.get("tipo") not in _TIPOS_POSITIVOS:
                        continue
                    if float(c.get("kg") or 0) <= 0:
                        continue
                    ai_usd_abs = abs(float(c.get("usd") or 0))
                    # Aceptamos si los USD coinciden ± 15% (IVA tolerancia)
                    # PERO no más de $5 absoluto en cifras chicas.
                    margen = max(pc_imp_abs * 0.15, 5.0)
                    if abs(ai_usd_abs - pc_imp_abs) <= margen:
                        ok.append(c)
                if len(ok) == 1:
                    r_ai = ok[0]
                    signo_invertido = True
        if r_ai is not None:
            f["asinfo_kg"] = float(r_ai.get("kg") or 0)
            f["asinfo_usd"] = float(r_ai.get("usd") or 0)
            f["asinfo_diff_kg"] = round(f["asinfo_kg"] - pc_kg, 3)
            f["asinfo_diff_usd"] = round(f["asinfo_usd"] - float(f.get("importe") or 0), 2)
            f["asinfo_tipo"] = r_ai.get("tipo")
            f["asinfo_signo_invertido"] = signo_invertido
