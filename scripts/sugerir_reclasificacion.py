#!/usr/bin/env python3
"""Sugerir reclasificación de xgast sin num — TMT 2026-05-19 v5.

Lista los top N conceptos de xgast con `num IS NULL` (o num fuera de
1..9), agrupados por concepto único, ordenados por monto.

Opcional: aplicar un mapping {concepto → V} desde un JSON o CSV.
Idempotente — solo afecta filas que tengan num NULL/0.
Dry-run por default; pasar `--apply` para ejecutar.

Uso:
    python scripts/sugerir_reclasificacion.py                    # listar top 50
    python scripts/sugerir_reclasificacion.py --top 100          # listar top 100
    python scripts/sugerir_reclasificacion.py --mapping map.json # dry-run
    python scripts/sugerir_reclasificacion.py --mapping map.json --apply

Formato del JSON:
    {"luz mayo": 8, "EEQ MARZO": 5, "EMAAP": 5, "agua": 8, ...}
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Permite correrlo desde la raíz del repo.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402


def listar_top(limite: int) -> list[dict]:
    return db.fetch_all(
        """
        SELECT COALESCE(NULLIF(TRIM(concepto), ''), '(sin concepto)') AS concepto,
               COUNT(*)                          AS n,
               COALESCE(SUM(importe), 0)         AS total,
               MIN(fecha)                        AS primera_fecha,
               MAX(fecha)                        AS ultima_fecha
          FROM scintela.xgast
         WHERE (num IS NULL OR num = 0 OR num NOT BETWEEN 1 AND 9)
           AND COALESCE(stat, '') <> 'Y'
         GROUP BY COALESCE(NULLIF(TRIM(concepto), ''), '(sin concepto)')
         ORDER BY SUM(importe) DESC, COUNT(*) DESC
         LIMIT %s
        """,
        (limite,),
    ) or []


def resumen() -> dict:
    return db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total,
               COUNT(DISTINCT COALESCE(concepto, '')) AS n_conceptos
          FROM scintela.xgast
         WHERE (num IS NULL OR num = 0 OR num NOT BETWEEN 1 AND 9)
           AND COALESCE(stat, '') <> 'Y'
        """
    ) or {}


def aplicar_mapping(mapping: dict, dry_run: bool, usuario: str) -> dict:
    """Aplica el mapping concepto→num. Idempotente: solo toca filas con
    num NULL/0/inválido."""
    aplicados = 0
    filas_total = 0
    importe_total = 0.0
    for concepto, num in mapping.items():
        try:
            num_int = int(num)
        except (TypeError, ValueError):
            print(f"  [skip] num inválido para {concepto!r}: {num!r}")
            continue
        if num_int < 1 or num_int > 9:
            print(f"  [skip] num fuera de 1..9 para {concepto!r}: {num_int}")
            continue
        # Pre-count
        pre = db.fetch_one(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
              FROM scintela.xgast
             WHERE TRIM(COALESCE(concepto, '')) = %s
               AND (num IS NULL OR num = 0 OR num NOT BETWEEN 1 AND 9)
               AND COALESCE(stat, '') <> 'Y'
            """,
            (concepto.strip(),),
        ) or {}
        n_pre = int(pre.get("n") or 0)
        total_pre = float(pre.get("total") or 0)
        if n_pre == 0:
            print(f"  [skip] {concepto!r}: 0 filas afectables (ya clasificadas o anuladas)")
            continue
        print(f"  {'[DRY]' if dry_run else '[APPLY]'} {concepto!r} → V{num_int}: "
              f"{n_pre} filas, $ {total_pre:,.2f}")
        if not dry_run:
            marca = f" [RECLASIF V{num_int} usr={usuario}]"
            db.execute(
                """
                UPDATE scintela.xgast
                   SET num = %s,
                       concepto = CASE
                           WHEN LENGTH(COALESCE(concepto, '')) + LENGTH(%s) <= 100
                           THEN COALESCE(concepto, '') || %s
                           ELSE concepto
                       END,
                       usuario_modifica = %s,
                       fecha_modifica = CURRENT_TIMESTAMP
                 WHERE TRIM(COALESCE(concepto, '')) = %s
                   AND (num IS NULL OR num = 0 OR num NOT BETWEEN 1 AND 9)
                   AND COALESCE(stat, '') <> 'Y'
                """,
                (num_int, marca, marca, usuario, concepto.strip()),
            )
        aplicados += 1
        filas_total += n_pre
        importe_total += total_pre

    return {
        "conceptos_aplicados": aplicados,
        "filas_total": filas_total,
        "importe_total": importe_total,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=50,
                        help="Cuántos conceptos top listar (default 50).")
    parser.add_argument("--mapping", type=str, default=None,
                        help="Path a JSON {concepto: num}. Si se pasa, aplica.")
    parser.add_argument("--apply", action="store_true",
                        help="Sin esto el mapping corre en dry-run.")
    parser.add_argument("--usuario", type=str, default="cli",
                        help="Usuario para audit trail (default 'cli').")
    args = parser.parse_args()

    r = resumen()
    print("=" * 70)
    print(f"xgast sin clasificar: {int(r.get('n') or 0)} filas · "
          f"$ {float(r.get('total') or 0):,.2f} · "
          f"{int(r.get('n_conceptos') or 0)} conceptos únicos")
    print("=" * 70)

    if args.mapping:
        with open(args.mapping) as f:
            mapping = json.load(f)
        print(f"\nAplicando mapping de {len(mapping)} conceptos "
              f"({'DRY-RUN' if not args.apply else 'APPLY'}):\n")
        out = aplicar_mapping(mapping, dry_run=not args.apply,
                              usuario=args.usuario)
        print("\n" + "─" * 70)
        print(f"Conceptos {('a aplicar' if not args.apply else 'aplicados')}: "
              f"{out['conceptos_aplicados']}")
        print(f"Filas afectadas: {out['filas_total']}")
        print(f"Importe total: $ {out['importe_total']:,.2f}")
        if not args.apply:
            print("\n(Pasá --apply para ejecutar realmente.)")
    else:
        print(f"\nTop {args.top} conceptos sin clasificar (por monto):\n")
        filas = listar_top(args.top)
        if not filas:
            print("  (no hay conceptos sin clasificar)")
            return
        print(f"  {'#':>4} {'$ total':>13} {'n':>5}  concepto")
        print(f"  {'-'*4} {'-'*13} {'-'*5}  {'-'*48}")
        for i, f in enumerate(filas, 1):
            print(f"  {i:>4} {float(f['total']):>13,.2f} {int(f['n']):>5}  "
                  f"{f['concepto'][:48]}")


if __name__ == "__main__":
    main()
