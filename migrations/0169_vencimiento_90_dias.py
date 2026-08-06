"""Vencimiento de facturas: 90 días en vez de 30.

TMT 2026-08-05 (dueña): el plazo default pasa de 30 a 90 días, y las
facturas IMPAGAS existentes se recalculan para que dejen de figurar
vencidas ("que todos esos vencidos no aparezcan tanto").

Regla (la misma que usa `facturas.queries.crear` desde hoy):
    vencimiento = fecha + (cliente.pago si es numérico, si no 90) días

- Sólo facturas pendientes: stat NULL/''/' '/'Z'/'A' y saldo <> 0.
- Sólo se MUEVE HACIA ADELANTE: GREATEST(vencimiento actual, calculado).
  Un vencimiento cargado a mano más lejano no se acorta nunca.
- La dueña cree que ningún cliente tiene plazo propio cargado; si alguno
  lo tuviera (pago numérico), se respeta — igual que en el alta.

Idempotente: la segunda corrida actualiza cero filas.
"""

from __future__ import annotations

import os


def run(conn) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE scintela.factura f
           SET vencimiento = GREATEST(
                   COALESCE(f.vencimiento, f.fecha),
                   f.fecha + (
                       CASE WHEN TRIM(COALESCE(c.pago, '')) ~ '^[0-9]+$'
                            THEN TRIM(c.pago)::int
                            ELSE 90 END
                   )
               )
          FROM scintela.cliente c
         WHERE c.codigo_cli = f.codigo_cli
           AND (f.stat IS NULL OR f.stat IN ('Z', 'A', '', ' '))
           AND COALESCE(f.saldo, 0) <> 0
           AND COALESCE(f.vencimiento, f.fecha) < f.fecha + (
                   CASE WHEN TRIM(COALESCE(c.pago, '')) ~ '^[0-9]+$'
                        THEN TRIM(c.pago)::int
                        ELSE 90 END
               )
        """
    )
    movidas = cur.rowcount

    # Facturas impagas cuyo cliente no existe en scintela.cliente (huérfanas
    # de código): mismo criterio con el default de 90.
    cur.execute(
        """
        UPDATE scintela.factura f
           SET vencimiento = GREATEST(
                   COALESCE(f.vencimiento, f.fecha), f.fecha + 90
               )
         WHERE NOT EXISTS (
                   SELECT 1 FROM scintela.cliente c
                    WHERE c.codigo_cli = f.codigo_cli
               )
           AND (f.stat IS NULL OR f.stat IN ('Z', 'A', '', ' '))
           AND COALESCE(f.saldo, 0) <> 0
           AND COALESCE(f.vencimiento, f.fecha) < f.fecha + 90
        """
    )
    huerfanas = cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    {movidas} factura(s) recalculadas a 90 días")
        print(f"    {huerfanas} sin cliente, recalculadas con el default")
