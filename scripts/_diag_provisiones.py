"""Diagnóstico: muestra qué posdat matcheó cada categoría de provisión.

Útil para entender por qué hay un gap residual vs dBase. Si una categoría
no encontró match, su provisión se perdió (no sumó nada).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from modules.informes.queries import PROVISIONES_DIARIAS, _condicion_provision  # noqa: E402


def main() -> int:
    print("PROVISIONES DIARIAS — ¿qué posdat matchea cada categoría?")
    print("=" * 90)
    total_match = 0
    total_monto = 0.0
    total_categorias = 0

    for prov_filter, matcher_kind, pattern, monto in PROVISIONES_DIARIAS:
        total_categorias += 1
        where_extra, params = _condicion_provision(prov_filter, matcher_kind, pattern)
        sql = f"""
            SELECT id_posdat, prov, concepto, importe, fechad
              FROM scintela.posdat
             WHERE COALESCE(banc, 0) <> 9
               AND {where_extra}
             ORDER BY id_posdat
             LIMIT 3
        """
        rows = db.fetch_all(sql, params) or []
        match_indicator = "✓" if rows else "✗"
        descrip = f"{prov_filter or 'all':>3} | {matcher_kind:<22} '{pattern}'"
        print(f"\n[{match_indicator}] {descrip}   → +${monto:,}")
        if rows:
            total_match += 1
            total_monto += monto
            for r in rows[:1]:
                imp = float(r.get("importe") or 0)
                print(f"      match: id_posdat={r['id_posdat']}  prov={r['prov']!r:6}  "
                      f"importe=${imp:,.2f}  concepto={r['concepto']!r}")
            if len(rows) > 1:
                print(f"      (+ {len(rows)-1} otras posibles matches — la primera por id_posdat es la que dBase tomaría)")
        else:
            print(f"      SIN MATCH — pattern no encuentra ningún posdat → falta ${monto:,} c/día")

    print()
    print("=" * 90)
    print(f"Categorías con match:   {total_match}/{total_categorias}")
    print(f"Monto sumando por día:  ${total_monto:,.2f}")
    print(f"Monto perdido por día:  ${31000 - total_monto:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
