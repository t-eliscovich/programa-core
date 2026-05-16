"""Backfill: cheques con stat='P' que NUNCA fueron postergados → stat='Z'.

Pedido TMT 2026-05-14: la regla nueva es que 'P' (Postergado) sólo aplica
cuando se movió la fecha de depósito DESPUÉS del vencimiento original.
Un cheque creado desde el inicio con fechad futura es 'Z' (En cartera),
no 'P'.

El alta de cheque modernos (post-2026-05-14) ya respeta esa regla:
`cheques.queries.crear` siempre asigna stat='Z' al alta. Este backfill
corrige las filas históricas que tienen stat='P' pero nunca fueron
postergadas (fecha_postergacion IS NULL).

USO:
    python scripts/backfill_postergado_a_cartera.py                       # dry-run
    python scripts/backfill_postergado_a_cartera.py --aplicar             # ejecuta
    python scripts/backfill_postergado_a_cartera.py --max-plazo-dias 180  # acotar

Criterio:
    stat = 'P'
    AND fecha_postergacion IS NULL AND fechad_original IS NULL
    AND (fechad - fecha) <= max_plazo_dias   ← cheques de plazo razonable

DEFAULT max_plazo_dias=365: catch cheques recibidos con fechad futura
"normal". Cheques con plazo > 365 días probablemente fueron postergados
varias veces en el dBase legacy (la columna fecha_postergacion no existía
entonces), por eso quedan EXCLUIDOS del migrado y mantienen stat='P'.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def encontrar_candidatos(max_plazo_dias: int = 365) -> list[dict]:
    """Cheques stat='P' que nunca fueron postergados y tienen plazo razonable."""
    return db.fetch_all(
        """
        SELECT
            c.id_cheque, c.no_cheque, c.codigo_cli,
            COALESCE(cli.nombre, '') AS cliente,
            c.fecha, c.fechad, c.importe, c.stat,
            c.fecha_postergacion, c.fechad_original,
            (c.fechad - c.fecha) AS plazo_dias
        FROM scintela.cheque c
        LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
        WHERE c.stat = 'P'
          AND c.fecha_postergacion IS NULL
          AND c.fechad_original IS NULL
          AND c.fecha IS NOT NULL
          AND c.fechad IS NOT NULL
          AND (c.fechad - c.fecha) BETWEEN 0 AND %s
        ORDER BY c.id_cheque
        """,
        (int(max_plazo_dias),),
    ) or []


def aplicar_backfill(rows: list[dict], usuario: str = "backfill") -> int:
    """Marca stat='Z' (En cartera) — el cheque sigue vivo, sólo cambia label."""
    n = 0
    for r in rows:
        id_cheque = int(r["id_cheque"])
        marca = "[Z] backfill 2026-05-14: nunca fue postergado, era cartera con fechad futura"
        with db.tx() as conn:
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat='Z', "
                "    observacion = RIGHT(COALESCE(observacion || ' | ', '') || %s, 200), "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s AND stat='P' "
                "  AND fecha_postergacion IS NULL "
                "  AND fechad_original IS NULL",
                (marca, usuario, id_cheque),
                conn=conn,
            )
        n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--aplicar", action="store_true",
                   help="ejecuta los UPDATEs (sin esto, dry-run)")
    p.add_argument("--usuario", default="backfill",
                   help="usuario_modifica a registrar")
    p.add_argument("--max-plazo-dias", type=int, default=365,
                   help="cheques con plazo (fechad-fecha) mayor a esto quedan como 'P' "
                        "(asumimos que fueron postergados en dBase legacy). Default 365.")
    args = p.parse_args()

    print(f"Buscando cheques stat='P' con plazo (fechad-fecha) ≤ {args.max_plazo_dias} días...")
    candidatos = encontrar_candidatos(max_plazo_dias=args.max_plazo_dias)
    if not candidatos:
        print("No se encontraron cheques candidatos. Nada para hacer.")
        return 0

    print(f"Encontrados {len(candidatos)} cheque(s) con stat='P' que nunca fueron postergados:")
    print()
    total_imp = 0.0
    for r in candidatos:
        imp = float(r.get("importe") or 0)
        total_imp += imp
        delta = ""
        if r.get("fecha") and r.get("fechad"):
            try:
                d = (r["fechad"] - r["fecha"]).days
                delta = f" (plazo {d}d)"
            except Exception:
                pass
        print(f"  id={r['id_cheque']:>5}  N°={r.get('no_cheque') or '—':>10}  "
              f"fecha={r['fecha']}  fechad={r['fechad']}{delta}")
        print(f"         importe=${imp:>10,.2f}  "
              f"{r.get('codigo_cli','')} {r.get('cliente','')[:30]}")
    print()
    print(f"Total importe a re-clasificar como En cartera: ${total_imp:,.2f}")
    print()

    if not args.aplicar:
        print("DRY-RUN. Ningún cambio aplicado.")
        print("Para ejecutar:  python scripts/backfill_postergado_a_cartera.py --aplicar")
        return 0

    n = aplicar_backfill(candidatos, usuario=args.usuario)
    print(f"✓ Aplicado: {n} cheque(s) re-clasificado(s) como stat='Z' (En cartera).")
    print("Ahora aparecen con badge 'En cartera' en /cheques.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
