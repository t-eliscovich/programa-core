-- 0019_backfill_saldo_running.sql
-- TMT 2026-05-11: el trigger de 0018 cubre INSERTs futuros pero las filas
-- viejas con saldo=NULL/0 no se arreglan solas. Esto las arregla en un
-- solo shot con un window function — el equivalente SQL del walk-forward
-- de bank_helpers.recompute_saldos_desde, pero corriendo todo a la vez.
--
-- Idempotente: si todo está bien, vuelve a escribir los mismos valores.
--
-- La convención de signo es la misma que el trigger y bank_helpers:
--   documento ∈ ('DE','TR','XX','NC','IN') → suma  (+1)
--   cualquier otro                          → resta (-1)

WITH running AS (
  SELECT
    id_transaccion,
    SUM(
      CASE
        WHEN UPPER(TRIM(COALESCE(documento, '')))
             IN ('DE','TR','XX','NC','IN') THEN  ABS(COALESCE(importe, 0))
        ELSE                                    -ABS(COALESCE(importe, 0))
      END
    ) OVER (
      PARTITION BY no_banco, COALESCE(no_cta, '')
      ORDER BY fecha, id_transaccion
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS saldo_calculado
  FROM scintela.transacciones_bancarias
)
UPDATE scintela.transacciones_bancarias t
   SET saldo = ROUND(r.saldo_calculado::numeric, 2),
       fecha_modifica = CURRENT_TIMESTAMP,
       usuario_modifica = COALESCE(t.usuario_modifica, 'migration-0019')
  FROM running r
 WHERE t.id_transaccion = r.id_transaccion
   AND (
     t.saldo IS DISTINCT FROM ROUND(r.saldo_calculado::numeric, 2)
   );
