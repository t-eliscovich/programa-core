#!/usr/bin/env python3
"""Para las 18 huerfanas: buscar en card 199 (cache in-memory) por (fecha, importe)
ignorando cliente. Si Asinfo tiene la fila con OTRO cliente_codigo o kg diferente,
ahi esta el problema."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv; load_dotenv(_p, override=False)
        except ImportError: pass
        break

import db; db.init_pool()
from modules.asinfo import service as asinfo_service


def _norm(s): return (s or "").strip().upper()


def matcher_view_que_queda():
    """Replica view + extrae las que quedan como huerfanas."""
    filas = db.fetch_all("""
        SELECT id_factura, numf, numf_completo, codigo_cli, fecha, kg, importe
          FROM scintela.factura
         WHERE stat IN ('Z','A') AND saldo > 0
           AND fecha >= '2025-01-01' AND kg <> 0
         ORDER BY fecha DESC, id_factura DESC
    """)
    rows = asinfo_service.facturas_periodo(date(2025,1,1), date(2026,5,28))
    _POS = ("FACTURA","NTEN","NC_FINANCIERA")
    _NEG = ("DEVOLUCION","NCNT")
    idx_c, idx_s, idx_cmp = {}, {}, defaultdict(list)
    for r in rows:
        t = r.get("tipo"); n = r.get("numero")
        if not n: continue
        if t in _POS or t in _NEG:
            if n not in idx_c: idx_c[n] = r
            sfx = n.split("-")[-1] if "-" in n else n
            try:
                si = int(sfx)
                if si not in idx_s: idx_s[si] = r
            except: pass
            cli = _norm(r.get("cliente_codigo"))
            fai = str(r.get("fecha"))[:10] if r.get("fecha") else None
            try: kg = float(r.get("kg") or 0)
            except: kg = 0
            if cli and fai:
                idx_cmp[(cli, fai, round(kg,2))].append(r)
    huerfanas = []
    for f in filas:
        try: pck = float(f.get("kg") or 0)
        except: pck = 0
        if pck == 0: continue
        if (f.get("numf_completo") or "").startswith("#"): continue
        nc = (f.get("numf_completo") or "").strip()
        rai = idx_c.get(nc) if nc else None
        if rai is None and f.get("numf"):
            try: rai = idx_s.get(int(f["numf"]))
            except: pass
        if rai is None:
            cli = _norm(f.get("codigo_cli"))
            fp = str(f.get("fecha"))[:10] if f.get("fecha") else None
            if cli and fp:
                cands = idx_cmp.get((cli, fp, round(pck,2)), [])
                pci = float(f.get("importe") or 0)
                ok = [c for c in cands if abs(float(c.get("usd") or 0) - pci) < 0.5]
                if len(ok) == 1: rai = ok[0]
        if rai is None: huerfanas.append(f)
    return huerfanas, rows


def main():
    huerfanas, asinfo_rows = matcher_view_que_queda()
    print(f"Huerfanas reales: {len(huerfanas)}")
    print(f"Asinfo rows en cache: {len(asinfo_rows)}\n")

    # Construir indices fuzzy sobre el cache
    # Por (fecha, usd_redondeado) sin cli
    idx_fecha_usd = defaultdict(list)
    idx_fecha = defaultdict(list)
    for r in asinfo_rows:
        f = str(r.get("fecha") or "")[:10]
        try: u = float(r.get("usd") or 0)
        except: u = 0
        if f:
            idx_fecha_usd[(f, round(u, 2))].append(r)
            idx_fecha[f].append(r)

    for h in huerfanas[:30]:
        pci = float(h["importe"] or 0)
        fp = str(h["fecha"])[:10]
        cli = _norm(h["codigo_cli"])
        print("=" * 100)
        print(f"PC id={h['id_factura']} cli={cli} fecha={fp} kg={float(h['kg'])} imp={pci}")

        # Estrategia 1: (fecha, usd exacto) sin cli
        cands = idx_fecha_usd.get((fp, round(pci, 2)), [])
        if cands:
            print(f"  [F1] Asinfo tiene {len(cands)} candidatos (fecha+usd exacto, cualquier cli):")
            for c in cands[:3]:
                print(f"     -> num={c.get('numero')} cli={c.get('cliente_codigo')} kg={c.get('kg')} usd={c.get('usd')} tipo={c.get('tipo')}")
            continue
        # Estrategia 2: fecha sola, ordenar por |usd - pci|
        all_fecha = idx_fecha.get(fp, [])
        if all_fecha:
            scored = sorted(all_fecha, key=lambda r: abs(float(r.get("usd") or 0) - pci))[:5]
            print(f"  [F2] {len(all_fecha)} filas Asinfo para esa fecha. Top 5 mas cercanos en usd:")
            for c in scored:
                d = float(c.get("usd") or 0) - pci
                print(f"     -> num={c.get('numero')} cli={c.get('cliente_codigo')} usd={c.get('usd')} kg={c.get('kg')} diff_usd={d:.2f} tipo={c.get('tipo')}")
            continue
        # Estrategia 3: fecha+/-2 dias + usd cercano
        cercanos = []
        for delta in (-2,-1,1,2):
            try:
                d0 = (date.fromisoformat(fp) + timedelta(days=delta)).isoformat()
                cercanos.extend(idx_fecha.get(d0, []))
            except: pass
        if cercanos:
            scored = sorted(cercanos, key=lambda r: abs(float(r.get("usd") or 0) - pci))[:5]
            print("  [F3] sin filas en fecha exacta. Top 5 cercanos en +/-2 dias:")
            for c in scored:
                d = float(c.get("usd") or 0) - pci
                print(f"     -> num={c.get('numero')} cli={c.get('cliente_codigo')} fecha={str(c.get('fecha'))[:10]} usd={c.get('usd')} kg={c.get('kg')} diff_usd={d:.2f}")
            continue
        print("  >>> NADA encontrado ni con fecha+/-2 dias <<<")


if __name__ == "__main__":
    main()
