"""Auditoría READ-ONLY de scintela.chequesxfact — busca aplicaciones de cheque
inconsistentes antes de decidir si conviene endurecer la tabla con constraints.

Contexto (bug hunt 2026-06-05, lente 7): la tabla NO tiene
UNIQUE(id_cheque, id_fact), y los reversos no tienen token de idempotencia, así
que EN TEORÍA un cheque podría aplicarse de más. PERO el código guarda una fila
por aplicación a propósito ("granularidad histórica"), así que varias filas por
(cheque, factura) pueden ser LEGÍTIMAS (pagos parciales). Por eso NO alcanza con
contar pares duplicados — hay que mirar las señales reales:

  A. Cheque SOBRE-APLICADO: Σ(chequesxfact.importe) > cheque.importe.
  B. Factura SOBRE-ABONADA por cheques: Σ aplicado > factura.importe.
  C. Filas EXACTAMENTE duplicadas (mismo id_cheque + id_fact + importe + fecha):
     huelen a doble-INSERT por reintento, no a pago parcial.

No escribe NADA. Devuelve exit code 1 si encuentra algo (útil para cron/CI),
0 si está limpio.

Uso:
    python scripts/audit_chequesxfact.py            # resumen + ejemplos
    python scripts/audit_chequesxfact.py --limit 50 # más ejemplos por check
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

import db  # noqa: E402

TOL = 0.01  # tolerancia de centavo para comparaciones de dinero.

SQL_CHEQUE_SOBRE_APLICADO = """
    SELECT cf.id_cheque,
           COALESCE(c.importe, 0)        AS cheque_importe,
           SUM(COALESCE(cf.importe, 0))  AS aplicado,
           COUNT(*)                      AS n_apps
      FROM scintela.chequesxfact cf
      JOIN scintela.cheque c ON c.id_cheque = cf.id_cheque
     GROUP BY cf.id_cheque, c.importe
    HAVING SUM(COALESCE(cf.importe, 0)) > COALESCE(c.importe, 0) + %(tol)s
     ORDER BY SUM(COALESCE(cf.importe, 0)) - COALESCE(c.importe, 0) DESC
     LIMIT %(limit)s
"""

SQL_FACTURA_SOBRE_ABONADA = """
    SELECT cf.id_fact,
           COALESCE(f.importe, 0)        AS factura_importe,
           SUM(COALESCE(cf.importe, 0))  AS aplicado_cheques,
           COUNT(*)                      AS n_apps
      FROM scintela.chequesxfact cf
      JOIN scintela.factura f ON f.id_factura = cf.id_fact
     GROUP BY cf.id_fact, f.importe
    HAVING SUM(COALESCE(cf.importe, 0)) > COALESCE(f.importe, 0) + %(tol)s
     ORDER BY SUM(COALESCE(cf.importe, 0)) - COALESCE(f.importe, 0) DESC
     LIMIT %(limit)s
"""

SQL_FILAS_DUPLICADAS = """
    SELECT id_cheque, id_fact,
           COALESCE(importe, 0) AS importe, fechaing,
           COUNT(*)             AS n
      FROM scintela.chequesxfact
     GROUP BY id_cheque, id_fact, COALESCE(importe, 0), fechaing
    HAVING COUNT(*) > 1
     ORDER BY COUNT(*) DESC
     LIMIT %(limit)s
"""


def _run(sql: str, limit: int) -> list[dict]:
    return db.fetch_all(sql, {"tol": TOL, "limit": limit}) or []


def _print_seccion(titulo: str, filas: list[dict], columnas: list[str]) -> None:
    print(f"\n── {titulo} ──")
    if not filas:
        print("   ✓ nada")
        return
    print("   " + " | ".join(columnas))
    for r in filas:
        print("   " + " | ".join(str(r.get(c, "")) for c in columnas))
    print(f"   ⚠ {len(filas)} fila(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25,
                        help="máximo de ejemplos por check (default 25).")
    args = parser.parse_args(argv)

    db.init_pool()

    sobre_aplicados = _run(SQL_CHEQUE_SOBRE_APLICADO, args.limit)
    sobre_abonadas = _run(SQL_FACTURA_SOBRE_ABONADA, args.limit)
    duplicadas = _run(SQL_FILAS_DUPLICADAS, args.limit)

    print("=" * 60)
    print("AUDITORÍA chequesxfact (read-only)")
    print("=" * 60)
    _print_seccion(
        "A. Cheques SOBRE-APLICADOS (Σ aplicado > importe del cheque)",
        sobre_aplicados,
        ["id_cheque", "cheque_importe", "aplicado", "n_apps"],
    )
    _print_seccion(
        "B. Facturas SOBRE-ABONADAS por cheques (Σ aplicado > importe factura)",
        sobre_abonadas,
        ["id_fact", "factura_importe", "aplicado_cheques", "n_apps"],
    )
    _print_seccion(
        "C. Filas EXACTAMENTE duplicadas (mismo cheque+factura+importe+fecha)",
        duplicadas,
        ["id_cheque", "id_fact", "importe", "fechaing", "n"],
    )

    total = len(sobre_aplicados) + len(sobre_abonadas) + len(duplicadas)
    print("\n" + "=" * 60)
    if total == 0:
        print("✓ LIMPIO — no se encontraron inconsistencias.")
        return 0
    print(f"⚠ {total} grupo(s) a revisar. NADA se modificó (auditoría read-only).")
    print("  Si A o B tienen filas, hubo sobre-aplicación real → revisar antes")
    print("  de cualquier constraint. Si sólo C, son probables dobles-INSERT.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
