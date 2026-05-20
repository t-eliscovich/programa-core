"""Diagnóstico: distribución de scintela.proveedor.tipo.

Uso (en EC2 vía SSM):
    Set-Location C:\\programa-core
    C:\\Python312\\python.exe scripts\\check_tipos_proveedor.py

Imprime un conteo por tipo (U/H/Q/B/Y/vacío) + algunos ejemplos.
"""
from __future__ import annotations

import os
import sys

# Hacer importable el package del repo.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db


def main() -> int:
    print("=== Distribución de scintela.proveedor.tipo ===")
    rows = db.fetch_all(
        """
        SELECT COALESCE(NULLIF(TRIM(tipo), ''), '(vacio)') AS t,
               COUNT(*) AS n
          FROM scintela.proveedor
         GROUP BY 1
         ORDER BY n DESC
        """
    ) or []
    if not rows:
        print("(sin proveedores en la base)")
        return 0
    total = sum(int(r["n"]) for r in rows)
    print(f"Total proveedores: {total}\n")
    print(f"{'tipo':10s}  {'n':>6s}  {'%':>6s}")
    print("-" * 28)
    for r in rows:
        t = (r["t"] or "").strip() or "(vacio)"
        n = int(r["n"])
        pct = 100.0 * n / total if total else 0.0
        print(f"{t:10s}  {n:>6d}  {pct:>5.1f}%")

    # Ejemplos por tipo (top 3).
    print("\n=== Ejemplos por tipo (top 3 con mayor deuda viva) ===")
    for r in rows:
        t = (r["t"] or "").strip()
        sql_tipo = "= %s" if t else "IS NULL OR TRIM(tipo) = ''"
        ejs = db.fetch_all(
            f"""
            SELECT p.codigo_prov, p.nombre, p.tipo,
                   COALESCE(SUM(pd.importe), 0) AS deuda
              FROM scintela.proveedor p
              LEFT JOIN scintela.posdat pd
                     ON pd.prov = p.codigo_prov
                    AND COALESCE(pd.banc, 0) = 0
                    AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
             WHERE p.tipo {sql_tipo}
             GROUP BY p.codigo_prov, p.nombre, p.tipo
             ORDER BY deuda DESC
             LIMIT 3
            """,
            ((t,) if t else ()),
        ) or []
        if ejs:
            print(f"\n  tipo='{t or '(vacio)'}':")
            for e in ejs:
                print(f"    {e['codigo_prov']:5s} {e['nombre'][:40]:40s}  deuda ${float(e.get('deuda') or 0):>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
