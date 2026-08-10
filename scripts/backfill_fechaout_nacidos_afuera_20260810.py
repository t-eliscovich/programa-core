#!/usr/bin/env python3
"""Completa `fechaout` en los cheques que NACIERON fuera de cartera.

CONTEXTO. El 05/08 el depósito pasó a escribir la fecha en `fechaout`. La
tercera ruta —`cheques.queries.crear()`, el cheque que nace ya depositado
(90/91 → 'B') o ya cobrado en caja (99 → 'C')— siguió escribiendo sólo
`fechaing` hasta el fix del 10/08. Estas son las filas que quedaron en el
medio: entraron y salieron el mismo día, y les falta la fecha de salida.

MEDIDO EN PRODUCCIÓN EL 10/08/2026 (dry-run):

    stat  banco   n     total        fecha a escribir
    B     90      104   116.459,12   05→08/08   (= fechaing)
    C     99       13    17.351,96   06→07/08   (= fecha, no tienen fechaing)

En las 117 filas `fecha`, `fechaing` (cuando existe) y `fecha_crea::date`
son EL MISMO DÍA — no hay ninguna decisión que tomar sobre qué fecha va.

NO MUEVE NINGÚN NÚMERO. Medido sobre el TOTC as-of de los 11 días entre el
31/07 y el 10/08: Δ 0,00 en todos. `SQL_DIA_SALIDA` ya devolvía este mismo
día por el COALESCE, y la condición del balance as-of es aditiva.

NO TOCA `fechaing`: el resumen de cobranza del día agrupa por día de
INGRESO y se imprime para contabilidad.

Uso:
    python scripts/backfill_fechaout_nacidos_afuera_20260810.py            # dry-run
    python scripts/backfill_fechaout_nacidos_afuera_20260810.py --aplicar
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

CORTE = "2026-08-05"
USUARIO = "backfill-fechaout-20260810"

SELECT_OBJETIVO = """
    SELECT id_cheque, stat, no_banco, codigo_cli, importe,
           fecha, fechaing, fecha_crea::date AS creado,
           COALESCE(fechaing, fecha) AS fechaout_nueva
      FROM scintela.cheque
     WHERE fecha_crea::date >= %s
       AND UPPER(COALESCE(stat, '')) NOT IN ('Z','P','D','1','2','3')
       AND usuario_modifica IS NULL
       AND fechaout IS NULL
     ORDER BY stat, id_cheque
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true",
                    help="sin esto NO escribe nada")
    args = ap.parse_args()

    filas = db.fetch_all(SELECT_OBJETIVO, (CORTE,)) or []
    if not filas:
        print("Nada para completar — ningún cheque nacido fuera de cartera "
              "sin fechaout.")
        return 0

    por_grupo: dict[tuple, list] = {}
    for f in filas:
        por_grupo.setdefault((f["stat"], f["no_banco"]), []).append(f)

    print(f"{'stat':<5}{'banco':<7}{'n':>5}{'total':>15}   fechas")
    for (stat, banco), fs in sorted(por_grupo.items()):
        total = sum(float(f["importe"] or 0) for f in fs)
        f0 = min(f["fechaout_nueva"] for f in fs)
        f1 = max(f["fechaout_nueva"] for f in fs)
        print(f"{stat:<5}{banco:<7}{len(fs):>5}{total:>15,.2f}   {f0} → {f1}")

    # El freno: si alguna fila no tiene de dónde sacar la fecha, no se
    # escribe NINGUNA. Media limpieza es peor que ninguna.
    sin_fecha = [f for f in filas if not f["fechaout_nueva"]]
    if sin_fecha:
        print(f"\nABORTA: {len(sin_fecha)} fila(s) sin fecha de dónde sacar "
              f"la salida: {[f['id_cheque'] for f in sin_fecha][:10]}")
        return 1

    if not args.aplicar:
        print(f"\nDRY-RUN — {len(filas)} fila(s) cambiarían. "
              f"Con --aplicar se escriben.")
        return 0

    n = 0
    with db.tx() as conn:
        for f in filas:
            n += db.execute(
                """
                UPDATE scintela.cheque
                   SET fechaout = %s, usuario_modifica = %s,
                       fecha_modifica = CURRENT_TIMESTAMP
                 WHERE id_cheque = %s AND fechaout IS NULL
                """,
                (f["fechaout_nueva"], USUARIO, f["id_cheque"]),
                conn=conn,
            ) or 0
    print(f"\nOK — {n} fecha(s) de salida completadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
