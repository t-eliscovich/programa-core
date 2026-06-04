-- 0075_dedupe_histos_2.sql
-- Segundo cleanup de duplicados en banco_historicos_pendientes — TMT 2026-06-03.
--
-- Re-ejecución del dedupe de mig 0063 porque aparecieron 163 nuevos dupes
-- después del borrar-sesion + sync dBase:
--   1) Tamara subió extracto → 143 histos NEW (fuente='sesion:N')
--   2) Conciliaciones marcaron conciliado_en en histos pre-existentes (143)
--   3) borrar-sesion (bug orden 2a/2b) reseteó conciliado_en de los pre-existentes
--      SIN borrar los NEW de la sesión
--   4) sync dBase no toca histos — los 163 dupes quedaron
--
-- Resultado actual: 306 pendientes (debería ser 143). +163 dupes residuales.
--
-- Fix idéntico a 0063: mismo dedupe por firma completa, marca extras
-- conciliado_en = CURRENT_TIMESTAMP + conciliado_por = 'mig-0075-dedupe'.
-- Conserva la fila más vieja (id ASC). Sin tocar las 143 reales.
--
-- Idempotente.

BEGIN;

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
         GROUP BY no_banco, documento, tipo, monto, fecha
        HAVING COUNT(*) > 1
    ) t;

    RAISE NOTICE 'Mig 0075: pendientes pre=%, filas duplicadas a dedupear=%',
                 n_pre, n_a_dedupear;
END $$;

-- Firma completa = (no_banco, documento, tipo, monto, fecha).
-- Mantenemos la fila id ASC (la más vieja); las extras se marcan conciliadas.
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
       conciliado_por = 'mig-0075-dedupe'
  FROM ranked r
 WHERE h.id = r.id
   AND r.rn > 1;

DO $$
DECLARE
    n_post INTEGER;
BEGIN
    SELECT COUNT(*) INTO n_post
      FROM scintela.banco_historicos_pendientes
     WHERE conciliado_en IS NULL;
    RAISE NOTICE 'Mig 0075: pendientes post=%', n_post;
END $$;

COMMIT;
