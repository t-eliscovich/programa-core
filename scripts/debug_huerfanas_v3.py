#!/usr/bin/env python3
"""V3 — replica el view /facturas?vista=cartera&solo_huerfanas=1 EXACTO
y para cada huérfana real consulta Metabase directo (SQL ad-hoc) por numf.

Esto nos dice si la factura existe en Asinfo con OTRO cli/fecha/kg que el
matcher por compound key no encuentra.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from datetime import date

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
from modules._lib import metabase_client
from modules.facturas import queries as fact_q


def _norm(s): return (s or "").strip().upper()
def _f(d): return str(d)[:10] if d else None


def matcher_view(filas, asinfo_rows):
    """Replica exacta del matcher de views.py linea 990-1145."""
    _TIPOS_POS = ("FACTURA", "NTEN", "NC_FINANCIERA")
    _TIPOS_NEG = ("DEVOLUCION", "NCNT")

    idx_factura_completo = {}
    idx_factura_numf = {}
    idx_devolucion_completo = {}
    idx_devolucion_numf = {}
    idx_compuesto = defaultdict(list)

    for r in asinfo_rows:
        tipo = r.get("tipo")
        numero = r.get("numero")
        if not numero:
            continue
        if tipo in _TIPOS_POS:
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif tipo in _TIPOS_NEG:
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            continue
        if numero not in c_idx:
            c_idx[numero] = r
        sufijo = numero.split("-")[-1] if "-" in numero else numero
        try:
            sufijo_int = int(sufijo)
            if sufijo_int not in n_idx:
                n_idx[sufijo_int] = r
        except (ValueError, TypeError):
            pass

    for r in asinfo_rows:
        tipo = r.get("tipo")
        if tipo not in (_TIPOS_POS + _TIPOS_NEG):
            continue
        cli = _norm(r.get("cliente_codigo"))
        fecha_ai = _f(r.get("fecha"))
        try:
            kg_ai = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            kg_ai = 0.0
        if not (cli and fecha_ai):
            continue
        idx_compuesto[(cli, fecha_ai, round(kg_ai, 2))].append(r)

    matched = 0
    huerfanas = []
    for f in filas:
        try:
            pc_kg = float(f.get("kg") or 0)
        except Exception:
            pc_kg = 0.0
        if (f.get("numf_completo") or "").startswith("#"):
            continue
        if pc_kg > 0:
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif pc_kg < 0:
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            continue

        r_ai = None
        numero = (f.get("numf_completo") or "").strip()
        if numero:
            r_ai = c_idx.get(numero)
        if r_ai is None and f.get("numf"):
            try:
                r_ai = n_idx.get(int(f["numf"]))
            except (ValueError, TypeError):
                pass
        if r_ai is None:
            cli_pc = _norm(f.get("codigo_cli"))
            fecha_pc = f.get("fecha")
            if cli_pc and fecha_pc:
                key = (cli_pc, str(fecha_pc)[:10], round(pc_kg, 2))
                cands = idx_compuesto.get(key, [])
                pc_imp = float(f.get("importe") or 0)
                ok = [c for c in cands if abs(float(c.get("usd") or 0) - pc_imp) < 0.5]
                if len(ok) == 1:
                    r_ai = ok[0]

        if r_ai is None and pc_kg != 0:
            huerfanas.append(f)
        else:
            matched += 1

    return matched, huerfanas


def main():
    # 1. PC filas — cartera (mismo filtro que la view)
    filas = db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, codigo_cli, fecha, kg, importe, saldo, stat
          FROM scintela.factura
         WHERE stat IN ('Z','A')
           AND saldo > 0
           AND fecha >= '2025-01-01'
         ORDER BY fecha DESC, id_factura DESC
        """
    )
    print(f"PC filas cartera fecha>=2025: {len(filas)}")

    # 2. Asinfo via card 199
    asinfo_rows = asinfo_service.facturas_periodo(date(2025,1,1), date(2026,5,28))
    print(f"Asinfo card 199: {len(asinfo_rows)} filas")

    # 3. Correr matcher EXACTO del view
    matched, huerfanas = matcher_view(filas, asinfo_rows)
    print(f"Matched={matched}, Huérfanas reales={len(huerfanas)}\n")

    # 4. Para cada huérfana, query Metabase DIRECTO por numf en la tabla cruda
    #    asinfo. Vamos a usar fetch_dataset contra database_id=2 (Asinfo).
    #    Como no sabemos el schema exacto, probamos primero un SELECT contra
    #    la tabla 'factura' (nombre dBase típico) y vemos qué columnas trae.
    huerfanas_con_numf = [h for h in huerfanas if h.get("numf") and h["numf"] > 0]
    print(f"De las {len(huerfanas)} huérfanas, {len(huerfanas_con_numf)} tienen numf > 0")
    print("\n" + "=" * 100)
    print("Sample numfs huérfanas (todas):")
    print("=" * 100)
    for h in huerfanas:
        print(f"id={h['id_factura']} numf={h.get('numf')} numf_completo={h.get('numf_completo')} "
              f"cli={_norm(h.get('codigo_cli'))} fecha={_f(h.get('fecha'))} "
              f"kg={float(h.get('kg') or 0)} imp={float(h.get('importe') or 0)}")

    # 5. Probar query Metabase ad-hoc — descubrir el schema primero
    print("\n" + "=" * 100)
    print("Discovering Asinfo schema via Metabase fetch_dataset...")
    print("=" * 100)
    # Sample SQL — listar tablas en database 2 (Asinfo)
    sql_tables = """
        SELECT TOP 30 TABLE_SCHEMA, TABLE_NAME
          FROM INFORMATION_SCHEMA.TABLES
         WHERE TABLE_TYPE = 'BASE TABLE'
         ORDER BY TABLE_NAME
    """
    tables = metabase_client.fetch_dataset(2, sql_tables)
    print(f"Tablas Asinfo encontradas: {len(tables)}")
    for t in tables[:30]:
        print(f"  {t.get('TABLE_SCHEMA')}.{t.get('TABLE_NAME')}")


if __name__ == "__main__":
    main()
