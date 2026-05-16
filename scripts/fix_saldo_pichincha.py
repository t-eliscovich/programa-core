"""Fix puntual: recomputar saldo de la fila CH con saldo=0 en Pichincha.

Uso: python scripts/fix_saldo_pichincha.py
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bank_helpers  # noqa: E402
import db  # noqa: E402

bad = db.fetch_one(
    "SELECT id_transaccion, fecha, importe, documento "
    "FROM scintela.transacciones_bancarias "
    "WHERE no_banco=10 AND COALESCE(saldo,0)=0 AND documento='CH' "
    "ORDER BY id_transaccion DESC LIMIT 1"
)
if not bad:
    print("No encontré ninguna fila CH de Pichincha con saldo=0. Nada que hacer.")
    raise SystemExit(0)

print(f"Fila rota: id={bad['id_transaccion']}  fecha={bad['fecha']}  "
      f"importe={bad['importe']}  doc={bad['documento']}")

with db.tx() as conn:
    n = bank_helpers.recompute_saldos_desde(
        conn, no_banco=10, ancla_id=bad["id_transaccion"]
    )
print(f"Recomputadas {n} filas desde el ancla.")

fixed = db.fetch_one(
    "SELECT id_transaccion, importe, saldo "
    "FROM scintela.transacciones_bancarias "
    "WHERE id_transaccion = %s",
    (bad["id_transaccion"],),
)
print(f"Después: saldo={fixed['saldo']}  importe={fixed['importe']}")
