-- 0092_recompute_saldo_caja_match_resumen.sql
-- TMT 2026-06-11: corrige la mig 0091. El recompute de 0091 usaba
-- UPPER(TRIM(tipo)) + ABS(importe) (la convención del trigger 0022),
-- pero el histórico legacy tiene filas pre-2026-04-30 con importe
-- NEGATIVO y/o tipo no canónico, y la fórmula VERIFICADA contra el
-- dBase (queries.resumen() / saldo_actual(): inicial 80.053,99
-- + entradas 248.556,03 − salidas 308.286,06 = 20.323,96) usa el
-- importe FIRMADO tal cual y comparación exacta de tipo:
--     'E' → +importe · 'S' → −importe · otro → +importe
-- Con ABS, esas filas legacy divergen (la 0091 dejó la última fila en
-- −20.428,56, offset −40.752,52). Acá recomputamos con la MISMA
-- convención que resumen(), así la última fila = lo que muestra el hero.
--
-- SOLO recalcula la columna `saldo` (orden fecha ASC, id_caja ASC).
-- No toca fechas, tipos, importes ni filas. Idempotente.
--
-- NOTA: el opening no puede leerse del primer saldo almacenado (la 0091
-- ya lo pisó). Lo reconstruimos para que cierre la identidad de
-- resumen(): el saldo de la última fila debe ser opening + Σ deltas,
-- con opening = el valor que resumen() reportaba ANTES de la 0091.
-- Ese opening es un invariante del libro: 80.053,99 (verificado fila
-- por fila contra el dBase el 2026-06-10).

WITH base AS (
    SELECT id_caja,
           fecha,
           CASE WHEN tipo = 'E' THEN importe
                WHEN tipo = 'S' THEN -importe
                ELSE importe
           END AS delta
      FROM scintela.caja
),
running AS (
    SELECT b.id_caja,
           ROUND(
               80053.99
               + SUM(b.delta) OVER (
                     ORDER BY b.fecha ASC, b.id_caja ASC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
               2) AS saldo_nuevo
      FROM base b
)
UPDATE scintela.caja c
   SET saldo = r.saldo_nuevo
  FROM running r
 WHERE r.id_caja = c.id_caja
   AND c.saldo IS DISTINCT FROM r.saldo_nuevo;
