-- 0072: Backfill confirm_batch_id para matches viejos.
--
-- Matches creados en el mismo (no_banco, usuario, fecha+segundo) =
-- misma operación de conciliación → mismo batch_id sintético.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

WITH grupos AS (
    SELECT id,
           'legacy_' || no_banco || '_' || usuario || '_' ||
             to_char(date_trunc('second', creado_en), 'YYYYMMDDHH24MISS') AS bid
      FROM scintela.banco_conciliacion_match
     WHERE confirm_batch_id IS NULL
       AND deshecho_en IS NULL
)
UPDATE scintela.banco_conciliacion_match m
   SET confirm_batch_id = g.bid
  FROM grupos g
 WHERE m.id = g.id;
