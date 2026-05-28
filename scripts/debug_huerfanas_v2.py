#!/usr/bin/env python3
"""V2 — replica EXACTA del filtro de huérfanas del view /facturas?solo_huerfanas=1.

Para cada huérfana real (las que el view marca como sin match), mostramos:
    - PC: cli, fecha, kg, importe (con tipo de Python para detectar Decimal vs float)
    - Candidatos Asinfo por (cli, fecha, round(kg,2))
    - Para cada candidato: tipo, num, kg, usd, diff(pc_imp - usd), ratio(usd/pc_imp)
    - Si usd × 1.15 ≈ pc_imp → marcamos IVA

Esto debería decirnos por qué el filtro `abs(usd - pc_imp) < 0.5` rechaza el match.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv
            load_dotenv(_p, override=False)
        except ImportError:
            pass
        break

import db
db.init_pool()

from modules.asinfo import service as asinfo_service


def _norm(s): return (s or "").strip().upper()
def _f(d): return str(d)[:10] if d else None


def main():
    # PC: mismo filtro que la view aplica para vista=cartera + solo_huerfanas.
    # Cartera = stat in (Z, A) + saldo > 0 (igual que /cartera).
    huerfanas = db.fetch_all(
        """
        SELECT id_factura AS id, codigo_cli, fecha, kg, importe, numf, numf_completo, stat, saldo
          FROM scintela.factura
         WHERE stat IN ('Z','A')
           AND saldo > 0
           AND fecha >= '2025-01-01'
           AND kg <> 0
           AND (numf_completo IS NULL OR numf_completo='' OR NOT numf_completo LIKE '#%%')
         ORDER BY fecha DESC, id_factura DESC
        """
    )

    # Asinfo: mismo periodo
    rows = asinfo_service.facturas_periodo(date(2025,1,1), date(2026,5,28))
    _TIPOS_POS = ("FACTURA","NTEN","NC_FINANCIERA")
    _TIPOS_NEG = ("DEVOLUCION","NCNT")

    # Mismo indice que la view
    idx_completo = defaultdict(dict)  # universo_pos/neg -> {numero -> row}
    idx_sufijo   = defaultdict(dict)
    idx_compuesto = defaultdict(list)
    for r in rows:
        tipo = r.get("tipo")
        cli = _norm(r.get("cliente_codigo"))
        fecha_ai = _f(r.get("fecha"))
        try:
            kg_ai = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            kg_ai = 0.0
        if not (cli and fecha_ai):
            continue
        if tipo in _TIPOS_POS or tipo in _TIPOS_NEG:
            idx_compuesto[(cli, fecha_ai, round(kg_ai, 2))].append(r)

    # Simula el matcher de la view: para cada huerfana corre la MISMA logica
    # y reporta por qué NO matchea (es el caso interesante).
    no_match = []
    rechazadas_por_importe = []
    rechazadas_por_ambiguedad = []
    matchearian_si_relax = []

    for f in huerfanas:
        try:
            pc_kg = float(f["kg"] or 0)
        except Exception:
            pc_kg = 0.0
        try:
            pc_imp = float(f["importe"] or 0)
        except Exception:
            pc_imp = 0.0
        if pc_kg == 0:
            continue

        cli = _norm(f["codigo_cli"])
        fecha = _f(f["fecha"])
        key = (cli, fecha, round(pc_kg, 2))
        candidatos = idx_compuesto.get(key, [])

        # Filtros adicionales del matcher segun signo
        if pc_kg > 0:
            cand_relevantes = [c for c in candidatos if c.get("tipo") in _TIPOS_POS]
        else:
            cand_relevantes = [c for c in candidatos if c.get("tipo") in _TIPOS_NEG]

        # Aplico el filtro de importe del matcher
        ok = [c for c in cand_relevantes
              if abs(float(c.get("usd") or 0) - pc_imp) < 0.5]

        # Es huerfana real solo si el numf=0 (sin n° en PC) — porque el match
        # por numero directo no aplicaria de todos modos
        es_candidata_compound = not (f.get("numf_completo") or f.get("numf"))

        # Categorize
        if not cand_relevantes:
            no_match.append((f, candidatos))
            continue
        if len(ok) == 1:
            # ESTE matchearia OK — no es huerfana segun matcher de la view.
            continue
        # Pasa a este punto = view la considera huerfana
        # Razon: importe no entra en tolerancia, o multiple candidates
        diffs = [(c, float(c.get('usd') or 0) - pc_imp,
                  (float(c.get('usd') or 0) / pc_imp) if pc_imp else 0)
                 for c in cand_relevantes]
        rechazadas_por_importe.append((f, diffs))

    print(f"\nTotal facturas cartera + kg!=0 + fecha>=2025: {len(huerfanas)}")
    print(f"No tienen NINGUN candidato Asinfo por (cli,fecha,kg): {len(no_match)}")
    print(f"Tienen candidato pero matcher las rechaza (importe/ambig): {len(rechazadas_por_importe)}\n")

    print("="*100)
    print("MUESTRA: 30 huérfanas con candidato Asinfo (rechazadas por filtro de importe)")
    print("="*100)
    for f, diffs in rechazadas_por_importe[:30]:
        pc_imp = float(f["importe"] or 0)
        print(f"\nPC id={f['id']} cli={_norm(f['codigo_cli'])} fecha={_f(f['fecha'])} "
              f"kg={float(f['kg'])} imp={pc_imp}  numf={f.get('numf')}")
        for c, d, r in diffs[:3]:
            print(f"  -> Asinfo tipo={c.get('tipo')} num={c.get('numero')} "
                  f"kg={c.get('kg')} usd={c.get('usd')} | diff_usd_vs_pc={d:.2f} ratio={r:.4f}")
    print("\n" + "="*100)
    print("MUESTRA: primeras 30 de las 112 que NO TIENEN candidato Asinfo (cli,fecha,kg)")
    print("="*100)
    for f, cands in no_match[:30]:
        print(f"PC id={f['id']} cli={_norm(f['codigo_cli'])} fecha={_f(f['fecha'])} "
              f"kg={float(f['kg'])} imp={float(f['importe'] or 0)} numf={f.get('numf')}")

    # Resumen del IVA si todas las rechazadas tienen mismo patron
    if rechazadas_por_importe:
        ratios = []
        for f, diffs in rechazadas_por_importe:
            if diffs and float(f["importe"] or 0):
                # tomar el primer candidato
                _, _, r = diffs[0]
                if 0 < r < 2:
                    ratios.append(r)
        if ratios:
            import statistics
            print(f"\nRatio Asinfo_usd / PC_importe (n={len(ratios)}):")
            print(f"  mediana={statistics.median(ratios):.4f}")
            print(f"  min={min(ratios):.4f}  max={max(ratios):.4f}")
            print(f"  Si mediana ≈ 0.870 (=1/1.15) → PC tiene IVA, Asinfo no.")
            print(f"  Si mediana ≈ 1.0 → mismo nivel, problema de redondeo.")


if __name__ == "__main__":
    main()
