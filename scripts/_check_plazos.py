"""Sanity check: corre plazos_dbase() y compara con el snapshot del diag."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.informes import queries  # noqa: E402

if __name__ == "__main__":
    import json
    p = queries.plazos_dbase()
    print(f"PLAZ.COBR  = {p['cobro']:>4} días   (n_facturas = {p['n_facturas']})")
    print(f"PLAZ.DEUDA = {p['deuda']:>4} días   (n_posdat   = {p['n_posdat']})")
    print()
    print("dBase target: 32.9 / 96.7 → debería estar cerca")
    out = ROOT / "scripts" / "_diag" / "plazos_check.json"
    out.write_text(json.dumps(p, indent=2))
    print(f"\n→ {out}")
