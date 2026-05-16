"""Diagnóstico: muestra TODOS los cheques con no_cheque='000' y su stat.

Sirve para entender por qué uno reversado todavía aparece.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def main() -> int:
    rows = db.fetch_all(
        """
        SELECT id_cheque, no_cheque, fecha, fechad, codigo_cli,
               importe, stat, fechaout, observacion,
               usuario_modifica, fecha_modifica
          FROM scintela.cheque
         WHERE COALESCE(no_cheque, '') = '000'
           AND codigo_cli = 'PUE'
         ORDER BY id_cheque DESC
        """
    ) or []
    print(f"Cheques con no_cheque='000' AND codigo_cli='PUE': {len(rows)}")
    print()
    for r in rows:
        print(f"  id={r['id_cheque']:>5}  stat={r.get('stat')!r:6}  "
              f"importe=${float(r.get('importe') or 0):>10,.2f}  "
              f"fecha={r.get('fecha')}  fechad={r.get('fechad')}")
        if r.get("fechaout"):
            print(f"         fechaout={r.get('fechaout')} (← reversado/cerrado)")
        if r.get("observacion"):
            print(f"         observacion: {r.get('observacion')[:120]!r}")
        if r.get("usuario_modifica"):
            print(f"         última mod: {r.get('usuario_modifica')} @ {r.get('fecha_modifica')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
