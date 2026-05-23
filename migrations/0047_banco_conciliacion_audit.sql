-- 0047_banco_conciliacion_audit.sql
-- TMT 2026-05-23 — Auditoría de cómo se produjo cada match + soft-undo.
--
-- Columnas nuevas:
--   metodo        — cómo se generó el match:
--     'matched_auto'        — vino del scorer del matcher (default backfill)
--     'matched_manual'      — el usuario lo forzó desde el modal de match manual
--     'created_from_real'   — el usuario creó la tx BANCSIS desde un real_only
--     'real_only_ok'        — el usuario aceptó un real_only como diferencia legítima
--                             (sin crear tx) — ya existía en 'estado', se duplica
--                             en 'metodo' para auditar uniforme
--     'bancsis_only_ok'     — idem para bancsis_only
--   deshecho_en   — si el usuario hizo "Deshacer" en /banco/historial, se marca
--                   acá la timestamp y la fila NO se DELETEa (audit-trail).
--                   El matcher excluye filas con deshecho_en IS NULL, así que
--                   un "deshacer" hace que el mov vuelva a aparecer.
--
-- IDEMPOTENTE: usar IF NOT EXISTS para todas las alteraciones.

BEGIN;

ALTER TABLE scintela.banco_conciliacion_match
    ADD COLUMN IF NOT EXISTS metodo TEXT;

ALTER TABLE scintela.banco_conciliacion_match
    ADD COLUMN IF NOT EXISTS deshecho_en TIMESTAMP;

ALTER TABLE scintela.banco_conciliacion_match
    ADD COLUMN IF NOT EXISTS deshecho_por TEXT;

-- Backfill: todas las filas existentes son matched_auto (antes del cambio
-- el único origen posible era el scorer; los aceptados unilaterales heredan
-- de su `estado`).
UPDATE scintela.banco_conciliacion_match
   SET metodo = CASE
                  WHEN estado = 'real_only_ok'    THEN 'real_only_ok'
                  WHEN estado = 'bancsis_only_ok' THEN 'bancsis_only_ok'
                  ELSE 'matched_auto'
                END
 WHERE metodo IS NULL;

-- Los unique index existentes ahora deben permitir reuso de la firma cuando
-- el mov fue "deshecho". Sino, no podemos re-conciliar después de un undo.
-- Drop + recreate WHERE deshecho_en IS NULL.
DROP INDEX IF EXISTS scintela.ux_bcm_real_firma;
CREATE UNIQUE INDEX IF NOT EXISTS ux_bcm_real_firma
    ON scintela.banco_conciliacion_match
       (no_banco, real_fecha, real_documento, real_monto, real_tipo)
    WHERE real_documento IS NOT NULL AND deshecho_en IS NULL;

DROP INDEX IF EXISTS scintela.ux_bcm_bancsis;
CREATE UNIQUE INDEX IF NOT EXISTS ux_bcm_bancsis
    ON scintela.banco_conciliacion_match (id_transaccion)
    WHERE id_transaccion IS NOT NULL AND deshecho_en IS NULL;

CREATE INDEX IF NOT EXISTS ix_bcm_deshecho
    ON scintela.banco_conciliacion_match (deshecho_en)
    WHERE deshecho_en IS NULL;  -- partial idx para acelerar el filtro normal

COMMIT;
