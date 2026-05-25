#!/usr/bin/env python3
"""Backfill PC.numf_completo cruzando contra Asinfo por sufijo numérico.

PROBLEMA QUE RESUELVE:
    El DBF tiene `numf` (175763) pero NO `numf_completo` (NTEN-10462 o
    001-099-000175763). Después de cada sync DBF, las facturas nuevas
    quedan "huérfanas" en /facturas?solo_huerfanas=1 hasta que se las
    matchee contra Asinfo.

QUÉ HACE:
    1. Lee todas las facturas PC con numf>0 y numf_completo IS NULL/''
       desde 2025-01-01 (cutoff Asinfo).
    2. Trae todas las facturas de Asinfo en el rango cubierto.
    3. Para cada PC factura, busca un Asinfo numero cuyo sufijo numérico
       coincida con numf. Si la fecha y el codigo_cli también coinciden,
       UPDATE numf_completo = asinfo.numero.
    4. Imprime resumen: cuántas se matchearon, cuántas siguen huérfanas.

USO (standalone):
    cd /Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core
    .venv/bin/python scripts/backfill_numf_completo_from_asinfo.py
        [--dry-run]               # solo reporta, no UPDATE
        [--limit=500]             # max facturas a procesar
        [--desde=2025-01-01]      # cutoff custom

USO (post-sync DBF, vía SSM en EC2):
    Lo llama automáticamente sync_dbase_actual.py después del import.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Cargar .env si está
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv
            load_dotenv(_p, override=False)
        except ImportError:
            with open(_p) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() and k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        break


import db
from modules.asinfo import service as asinfo_service


ASINFO_CUTOFF = date(2025, 1, 1)


def _extract_numf(asinfo_numero: str) -> int | None:
    """Extrae el sufijo numérico de un numero de Asinfo.

    Ejemplos:
        '001-099-000175763' -> 175763
        'NTEN-10462'        -> 10462
        'NCNT-00123'        -> 123
        ''                  -> None
    """
    if not asinfo_numero:
        return None
    # Toma el último grupo de dígitos del string
    m = re.findall(r"\d+", str(asinfo_numero))
    if not m:
        return None
    try:
        return int(m[-1])
    except (ValueError, TypeError):
        return None


def cargar_huerfanas_pc(desde: date, limite: int) -> list[dict]:
    """Facturas PC con numf>0 pero numf_completo NULL/vacío."""
    return db.fetch_all(
        """
        SELECT id_factura, numf, fecha, codigo_cli, kg, importe
          FROM scintela.factura
         WHERE fecha >= %s
           AND numf IS NOT NULL AND numf > 0
           AND (numf_completo IS NULL OR numf_completo = '')
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
         ORDER BY fecha DESC
         LIMIT %s
        """,
        (desde, limite),
    )


def backfill(dry_run: bool = False, limite: int = 5000,
             desde: date | None = None) -> dict:
    """Run the backfill. Returns stats dict."""
    desde = desde or ASINFO_CUTOFF
    huerfanas = cargar_huerfanas_pc(desde, limite)
    n_huerf = len(huerfanas)
    if n_huerf == 0:
        return {"huerfanas_pc": 0, "matched": 0, "updated": 0, "ambig": 0, "no_match": 0}

    fechas = [h["fecha"] for h in huerfanas if h.get("fecha")]
    if not fechas:
        return {"huerfanas_pc": n_huerf, "matched": 0, "updated": 0, "ambig": 0, "no_match": n_huerf}

    mn = min(fechas).isoformat()
    mx = max(fechas).isoformat()

    try:
        asinfo_facts = asinfo_service.facturas_periodo(mn, mx)
    except Exception as e:
        print(f"ERR: Asinfo falló: {e}", file=sys.stderr)
        return {"huerfanas_pc": n_huerf, "matched": 0, "updated": 0,
                "ambig": 0, "no_match": n_huerf, "error": str(e)}

    # Indexar Asinfo por (numf_extraído, codigo_cli)
    # Si hay múltiples Asinfo con mismo numf+cli, marcamos ambiguo (raro).
    asinfo_by_key: dict[tuple[int, str], list[dict]] = {}
    for ai in asinfo_facts:
        numf = _extract_numf(ai.get("numero"))
        if numf is None:
            continue
        cli = (ai.get("cliente_codigo") or "").strip().upper()
        asinfo_by_key.setdefault((numf, cli), []).append(ai)

    matched = 0
    ambig = 0
    no_match = 0
    updates: list[tuple[str, int]] = []

    for h in huerfanas:
        numf = int(h["numf"])
        cli = (h.get("codigo_cli") or "").strip().upper()
        candidatos = asinfo_by_key.get((numf, cli), [])
        if not candidatos:
            # Fallback: probar sólo por numf, sin filtrar cliente
            candidatos_loose = [
                ai for key, lst in asinfo_by_key.items()
                if key[0] == numf for ai in lst
            ]
            if len(candidatos_loose) == 1:
                candidatos = candidatos_loose
        if not candidatos:
            no_match += 1
            continue
        if len(candidatos) > 1:
            # Múltiples Asinfo con mismo numf+cli — no actualizamos, requiere review humano
            ambig += 1
            continue
        # Match único -> encolar UPDATE
        ai = candidatos[0]
        updates.append((ai["numero"], int(h["id_factura"])))
        matched += 1

    # Aplicar UPDATEs
    updated = 0
    if updates and not dry_run:
        for numero, id_factura in updates:
            try:
                db.execute(
                    "UPDATE scintela.factura SET numf_completo = %s "
                    "WHERE id_factura = %s",
                    (numero, id_factura),
                )
                updated += 1
            except Exception as e:
                print(f"ERR: UPDATE {id_factura} falló: {e}", file=sys.stderr)

    return {
        "huerfanas_pc": n_huerf,
        "asinfo_facts": len(asinfo_facts),
        "matched": matched,
        "updated": updated,
        "ambig": ambig,
        "no_match": no_match,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="No UPDATE, solo reportar")
    ap.add_argument("--limit", type=int, default=5000, help="Max facturas a procesar")
    ap.add_argument("--desde", type=str, default=None, help="YYYY-MM-DD")
    args = ap.parse_args()

    desde = None
    if args.desde:
        from datetime import datetime
        desde = datetime.fromisoformat(args.desde).date()

    print(f"-> Backfill numf_completo (dry-run={args.dry_run}, limit={args.limit})")
    stats = backfill(dry_run=args.dry_run, limite=args.limit, desde=desde)

    print()
    print("==== Resumen ====")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    if stats["matched"] and not args.dry_run:
        print(f"OK {stats['updated']} facturas actualizadas con numf_completo.")
    if stats.get("no_match"):
        print(f"! {stats['no_match']} siguen huérfanas (no encontradas en Asinfo).")
    if stats.get("ambig"):
        print(f"! {stats['ambig']} ambiguas (múltiples Asinfo con mismo numf+cli) — review manual.")


if __name__ == "__main__":
    main()
