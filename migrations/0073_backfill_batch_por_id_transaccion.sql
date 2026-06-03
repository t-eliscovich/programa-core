-- 0073: backfill confirm_batch_id agrupando por id_transaccion compartido.
--
-- Los matches creados por agrupado de impuestos (crear_transaccion_agrupada_desde_reals)
-- antes de mig 0073 no tenían confirm_batch_id. Los N matches apuntan a la
-- misma id_transaccion (la tx agrupada). Los agrupamos retroactivamente.
--
-- Si ya tienen batch_id (lo asignó el flujo manual con confirm_batch_id),
-- no los tocamos. Solo backfill de NULL.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

WITH grupos_por_tx AS (
    SELECT id_transaccion,
           MIN(creado_en) AS primera,
           'autobatch_idtx_' || id_transaccion AS bid,
           COUNT(*) AS n
      FROM scintela.banco_conciliacion_match
     WHERE confirm_batch_id IS NULL
       AND id_transaccion IS NOT NULL
       AND deshecho_en IS NULL
     GROUP BY id_transaccion
    HAVING COUNT(*) > 1
)
UPDATE scintela.banco_conciliacion_match m
   SET confirm_batch_id = g.bid
  FROM grupos_por_tx g
 WHERE m.id_transaccion = g.id_transaccion
   AND m.confirm_batch_id IS NULL
   AND m.deshecho_en IS NULL;
