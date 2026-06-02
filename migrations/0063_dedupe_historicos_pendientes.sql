-- 0063_dedupe_historicos_pendientes.sql
-- Cleanup one-shot de duplicados en banco_historicos_pendientes — TMT 2026-06-02.
--
-- Dueña: 'sigo viendo todo esto' (548 pendientes a conciliar).
-- El dedupe por documento que pusimos en mig 0062 corre al subir extracto
-- NUEVO. Los 548 vienen del backfill viejo (migs 0056-0058) cuando no
-- había chequeo de documento. Esta migración limpia retroactivamente:
--
--   Para cada (no_banco, documento) con varios pendientes (conciliado_en IS NULL),
--   conservamos la fila MÁS VIEJA y marcamos las extras como
--   conciliado_en=CURRENT_TIMESTAMP + conciliado_por='mig-0063-dedupe'.
--
-- No borramos filas — solo las marcamos conciliadas, preservando auditoría.
-- Documentos vacíos NO se dedupean (sin clave para comparar).
--
-- Idempotente: si corrés dos veces, la segunda no cambia nada porque los
-- duplicados quedaron marcados conciliados en la primera.

BEGIN;

-- 1) Snapshot pre-cleanup para auditoría.
DO $$
DECLARE
    n_pre INTEGER;
    n_a_dedupear INTEGER;
BEGIN
    SELECT COUNT(*) INTO n_pre
      FROM scintela.banco_historicos_pendientes
     WHERE conciliado_en IS NULL;

    SELECT COALESCE(SUM(extras), 0) INTO n_a_dedupear FROM (
        SELECT COUNT(*) - 1 AS extras
          FROM scintela.banco_historicos_pendientes
         WHERE conciliado_en IS NULL
           AND documento IS NOT NULL
           AND documento <> ''
         GROUP BY no_banco, documento
        HAVING COUNT(*) > 1
    ) t;

    RAISE NOTICE 'Mig 0063: pendientes pre=%, filas duplicadas a dedupear=%',
                 n_pre, n_a_dedupear;
END $$;

-- 2) Marcar como conciliados los pendientes EXTRA — quedando 1 por
-- (no_banco, documento, tipo, monto). El "1 que queda" es la fila con
-- menor id (la más vieja).
--
-- ⚠ IMPORTANTE — TMT 2026-06-02 dueña: 'puede ser que esos no esten
-- duplicados entonces?'. Si dos filas tienen MISMO documento pero
-- DISTINTO tipo (C vs D), son un PAR CARGO+REVERSO legítimo (suma
-- contable = $0) — NO se deben dedupear. La partición incluye
-- `tipo` y `monto` para preservar esos pares.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY no_banco, documento, tipo, monto, fecha
               ORDER BY id ASC
           ) AS rn
      FROM scintela.banco_historicos_pendientes
     WHERE conciliado_en IS NULL
       AND documento IS NOT NULL
       AND documento <> ''
)
UPDATE scintela.banco_historicos_pendientes h
   SET conciliado_en = CURRENT_TIMESTAMP,
       conciliado_por = 'mig-0063-dedupe'
  FROM ranked r
 WHERE h.id = r.id
   AND r.rn > 1;

-- 3) Snapshot post-cleanup.
DO $$
DECLARE
    n_post INTEGER;
BEGIN
    SELECT COUNT(*) INTO n_post
      FROM scintela.banco_historicos_pendientes
     WHERE conciliado_en IS NULL;
    RAISE NOTICE 'Mig 0063: pendientes post=%', n_post;
END $$;

COMMIT;
