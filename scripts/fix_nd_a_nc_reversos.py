"""Cambia documento de 'ND' a 'NC' en reversos de cheque emitido + recomputa saldos.

Bug histórico: `bancos.reversar_cheque_emitido()` insertaba el reverso con
documento='ND' (Nota de Débito) pero ND es un EGRESO bancario en
`bank_helpers.signo_documento()`. Resultado: el saldo bajaba otra vez en
lugar de subir. Saldos viejos arrastran ese error.

Este script:
  1. Encuentra todas las filas con concepto LIKE 'REVERSO ch tx#%' y
     documento='ND' (formato que insertaba el bug).
  2. Cambia documento a 'NC'.
  3. Recomputa saldos running desde el banco afectado, anclando en la
     fila más vieja modificada.

USO:
    python scripts/fix_nd_a_nc_reversos.py            # dry-run
    python scripts/fix_nd_a_nc_reversos.py --apply    # ejecuta
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
import bank_helpers  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Aplica los cambios. Sin esto, dry-run.")
    args = p.parse_args()
    dry = not args.apply

    print(f"┌─ Fix ND→NC en reversos de cheque emitido — "
          f"{'DRY RUN' if dry else 'APLICANDO'}")
    print("└─\n")

    # 1) Encontrar las filas con ND que vinieron del flujo de reverso.
    filas = db.fetch_all(
        """
        SELECT id_transaccion, no_banco, fecha, importe, saldo, concepto
          FROM scintela.transacciones_bancarias
         WHERE UPPER(TRIM(documento)) = 'ND'
           AND concepto LIKE 'REVERSO ch tx#%%'
         ORDER BY no_banco, fecha, id_transaccion
        """
    ) or []

    if not filas:
        print("No hay filas ND con concepto 'REVERSO ch tx#%' — nada que arreglar.")
        return 0

    # Agrupar por banco para recomputar una sola vez por banco.
    por_banco: dict[int, list[dict]] = {}
    for r in filas:
        por_banco.setdefault(int(r["no_banco"]), []).append(r)

    print(f"Encontradas {len(filas)} filas ND mal en {len(por_banco)} bancos:\n")
    for nb, rs in por_banco.items():
        rs_ordenadas = sorted(rs, key=lambda x: (x["fecha"], x["id_transaccion"]))
        primera = rs_ordenadas[0]
        print(f"  Banco {nb}: {len(rs)} filas — más vieja id={primera['id_transaccion']} "
              f"fecha={primera['fecha']}")
        for r in rs_ordenadas[:5]:
            print(f"    id={r['id_transaccion']:>5} {r['fecha']} "
                  f"importe={r['importe']:>10,.2f} saldo_actual={r['saldo']:>14,.2f}  "
                  f"{r['concepto'][:50]}")
        if len(rs_ordenadas) > 5:
            print(f"    ... y {len(rs_ordenadas) - 5} más")

    if dry:
        print("\n⚠ DRY RUN. Volvé a correr con --apply para arreglar.")
        return 0

    # 2) APLICAR — un solo db.tx por banco para atomicidad.
    print("\n┌─ APLICANDO:")
    for nb, rs in por_banco.items():
        rs_ordenadas = sorted(rs, key=lambda x: (x["fecha"], x["id_transaccion"]))
        primera = rs_ordenadas[0]
        ids = [r["id_transaccion"] for r in rs]
        with db.tx() as conn:
            # Update doc ND → NC
            for id_tx in ids:
                db.execute(
                    "UPDATE scintela.transacciones_bancarias "
                    "SET documento='NC', "
                    "    fecha_modifica=CURRENT_TIMESTAMP, "
                    "    usuario_modifica='fix_nd_nc_script' "
                    "WHERE id_transaccion=%s",
                    (id_tx,),
                    conn=conn,
                )
            # Recomputar saldos desde la fila más vieja (ancla_id).
            n_recom = bank_helpers.recompute_saldos_desde(
                conn, no_banco=nb, ancla_id=primera["id_transaccion"],
            )
            print(f"│  Banco {nb}: {len(rs)} filas → NC, {n_recom} saldos recomputados desde id={primera['id_transaccion']}.")
    print("└─\n✓ Hecho.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
