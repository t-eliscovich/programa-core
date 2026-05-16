"""Test directo de correr_provisiones_diarias().

Uso:
    python scripts/_test_provisiones.py           # ver estado, no forzar
    python scripts/_test_provisiones.py --forzar  # forzar 1 día extra
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
from modules.informes import queries  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--forzar", action="store_true", help="aplicar UN día extra aunque ya esté al día")
    args = p.parse_args()

    # Estado actual
    meta = db.fetch_one(
        "SELECT valor FROM scintela.sistema_meta WHERE clave='provisiones_diarias_ult_fecha'"
    )
    print(f"ult_fecha actual: {(meta or {}).get('valor', '(no existe — primera vez)')}")

    pasivos_antes = db.fetch_one(
        "SELECT COALESCE(SUM(importe),0) AS t FROM scintela.posdat WHERE COALESCE(banc,0)<>9"
    )
    print(f"PASIVOS antes:  ${float((pasivos_antes or {}).get('t') or 0):,.2f}")
    print()

    result = queries.correr_provisiones_diarias(forzar=args.forzar)
    print("Resultado:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print()

    pasivos_despues = db.fetch_one(
        "SELECT COALESCE(SUM(importe),0) AS t FROM scintela.posdat WHERE COALESCE(banc,0)<>9"
    )
    print(f"PASIVOS después: ${float((pasivos_despues or {}).get('t') or 0):,.2f}")
    delta = float((pasivos_despues or {}).get('t') or 0) - float((pasivos_antes or {}).get('t') or 0)
    print(f"Δ aplicado: ${delta:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
