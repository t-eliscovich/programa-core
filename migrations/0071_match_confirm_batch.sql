-- 0071: confirm_batch_id en banco_conciliacion_match para deshacer grupos.
--
-- Hoy cada match es 1:1 en DB. Una conciliación 2 vs 14 crea 14 matches.
-- "Deshacer" libera de a 1 — si tenés N:M, la math se rompe.
--
-- Fix: confirm_batch_id (TEXT) agrupa los matches creados juntos en una
-- sola operación de conciliación. La UI ofrece "Deshacer grupo" que borra
-- todos los matches con el mismo batch_id.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

ALTER TABLE scintela.banco_conciliacion_match
    ADD COLUMN IF NOT EXISTS confirm_batch_id TEXT;

CREATE INDEX IF NOT EXISTS ix_bcm_batch_id
    ON scintela.banco_conciliacion_match (confirm_batch_id)
 WHERE confirm_batch_id IS NOT NULL;
