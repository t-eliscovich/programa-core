#!/usr/bin/env python3
"""ITEM #4 — Toma snapshot diario de la cartera por cliente.

Reemplaza el patrón legacy `COPY TO F:\\LUNES\\FACTURAS` de dBase
(MENU.PRG L1582-1660 PROCEDURE CONTROLC). Idempotente — re-correr el
mismo día sobreescribe la fila por cliente (ON CONFLICT DO UPDATE).

USO:
    python scripts/tomar_snapshot_cartera.py
    python scripts/tomar_snapshot_cartera.py --fecha 2026-05-14
    python scripts/tomar_snapshot_cartera.py --dry-run

Se recomienda agendarlo en cron a las 06:00:
    0 6 * * 1-6  cd /path/programa-core && venv/bin/python scripts/tomar_snapshot_cartera.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fecha", default=None,
                   help="Fecha del snapshot (YYYY-MM-DD). Default: hoy.")
    p.add_argument("--dry-run", action="store_true",
                   help="Sólo muestra cuántos clientes se snapshotearían.")
    args = p.parse_args()

    fecha = date.today()
    if args.fecha:
        try:
            fecha = datetime.fromisoformat(args.fecha).date()
        except ValueError:
            print(f"ERROR: --fecha inválida: {args.fecha!r}")
            return 2

    import db
    from modules.cartera import queries as q

    if args.dry_run:
        rows = db.fetch_all(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(saldo), 0) AS total
              FROM (
                SELECT f.codigo_cli, COALESCE(SUM(f.saldo), 0) AS saldo
                  FROM scintela.factura f
                 WHERE COALESCE(f.saldo, 0) > 0
                   AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
                 GROUP BY f.codigo_cli
              ) t
            """
        ) or [{}]
        r = rows[0] if rows else {}
        print(f"[DRY-RUN] {fecha.isoformat()} → {r.get('n') or 0} clientes, "
              f"saldo total {float(r.get('total') or 0):,.2f}")
        return 0

    res = q.tomar_snapshot(fecha)
    print(f"Snapshot {res['fecha']} OK: "
          f"{res['n_clientes']} clientes, "
          f"{res['n_filas_insertadas']} INSERT, "
          f"{res['n_filas_actualizadas']} UPDATE, "
          f"saldo total {res['saldo_total']:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
