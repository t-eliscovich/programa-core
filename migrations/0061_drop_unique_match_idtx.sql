-- 0061_drop_unique_match_idtx.sql
-- TMT 2026-05-29: el UNIQUE `ux_bcm_bancsis` en
-- `banco_conciliacion_match (id_transaccion)` bloqueaba el flujo N:1
-- del agrupado de impuestos: cuando se crea UNA tx BANCSIS y se
-- conciliarán N reales contra ESE id_transaccion, el primer INSERT
-- consumía el UNIQUE y los siguientes 3 caían en ON CONFLICT DO
-- NOTHING. Resultado: solo 1 match grababa de N.
--
-- El E2E real lo destapó (4 impuestos seleccionados, flash mostraba
-- "1 match(es) conciliados" en lugar de 4).
--
-- Fix: drop UNIQUE, recrear como INDEX normal (mantiene perf de
-- lookups por id_transaccion sin imponer unicidad).

BEGIN;

DROP INDEX IF EXISTS scintela.ux_bcm_bancsis;

-- Index no-UNIQUE para acelerar lookups (matcher pregunta "qué id_tx
-- ya están conciliados" para excluirlos del pool de candidatos).
CREATE INDEX IF NOT EXISTS ix_bcm_id_transaccion
    ON scintela.banco_conciliacion_match (id_transaccion)
    WHERE id_transaccion IS NOT NULL AND deshecho_en IS NULL;

COMMIT;
