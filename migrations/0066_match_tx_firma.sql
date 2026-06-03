-- 0066: tx_firma en banco_conciliacion_match para sobrevivir sync dBase.
--
-- Problema: cada sync DELETE+INSERT scintela.transacciones_bancarias.
-- El SERIAL id_transaccion cambia. Los matches existentes quedan apuntando
-- a ids muertos. Al recompute de pendientes, las filas "conciliadas" pasan
-- a "pendientes" otra vez, descuadrando el saldo. La dueña conciliaba 14 vs 2,
-- sincronizaba, y se rompía.
--
-- Fix: firma estable del lado PC en cada match:
--   tx_firma = fecha|documento|importe|numreferencia|concepto[:40]
--
-- Se setea via trigger BEFORE INSERT/UPDATE OF id_transaccion. Así todas las
-- rutas de INSERT (banco_v2_view, matcher_banco, scripts) la pueblan auto.
-- Post-sync, el relink busca la nueva id_transaccion por firma idéntica.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

ALTER TABLE scintela.banco_conciliacion_match
    ADD COLUMN IF NOT EXISTS tx_firma TEXT;

CREATE OR REPLACE FUNCTION scintela.compute_tx_firma_for_match()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id_transaccion IS NOT NULL AND NEW.tx_firma IS NULL THEN
        SELECT
            COALESCE(t.fecha::TEXT, '') || '|'
         || COALESCE(t.documento, '') || '|'
         || COALESCE(t.importe::TEXT, '0') || '|'
         || COALESCE(t.numreferencia::TEXT, '') || '|'
         || COALESCE(LEFT(t.concepto, 40), '')
          INTO NEW.tx_firma
          FROM scintela.transacciones_bancarias t
         WHERE t.id_transaccion = NEW.id_transaccion;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_bcm_compute_tx_firma ON scintela.banco_conciliacion_match;
CREATE TRIGGER tg_bcm_compute_tx_firma
BEFORE INSERT OR UPDATE OF id_transaccion ON scintela.banco_conciliacion_match
FOR EACH ROW EXECUTE FUNCTION scintela.compute_tx_firma_for_match();

-- Backfill: para matches existentes con id_transaccion vivo, computar firma.
UPDATE scintela.banco_conciliacion_match m
   SET tx_firma = COALESCE(t.fecha::TEXT, '') || '|'
                || COALESCE(t.documento, '') || '|'
                || COALESCE(t.importe::TEXT, '0') || '|'
                || COALESCE(t.numreferencia::TEXT, '') || '|'
                || COALESCE(LEFT(t.concepto, 40), '')
  FROM scintela.transacciones_bancarias t
 WHERE t.id_transaccion = m.id_transaccion
   AND m.tx_firma IS NULL
   AND m.id_transaccion IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bcm_tx_firma
    ON scintela.banco_conciliacion_match (tx_firma)
 WHERE tx_firma IS NOT NULL;

-- Funcion helper para post-sync relink. Recibe no_banco y devuelve cantidades.
CREATE OR REPLACE FUNCTION scintela.relink_matches_post_sync(p_no_banco INTEGER)
RETURNS TABLE(matches_total INTEGER, relinked INTEGER, sin_firma INTEGER, sin_match INTEGER) AS $$
DECLARE
    v_total INTEGER;
    v_relink INTEGER := 0;
    v_sinfirma INTEGER;
    v_sinmatch INTEGER := 0;
BEGIN
    SELECT COUNT(*) INTO v_total
      FROM scintela.banco_conciliacion_match
     WHERE no_banco = p_no_banco AND deshecho_en IS NULL;

    SELECT COUNT(*) INTO v_sinfirma
      FROM scintela.banco_conciliacion_match
     WHERE no_banco = p_no_banco AND deshecho_en IS NULL
       AND tx_firma IS NULL AND id_transaccion IS NOT NULL;

    -- Relink: para matches con firma cuya id_transaccion apunta a una fila
    -- que ya no existe, buscar nueva id_transaccion por firma.
    WITH dead_matches AS (
        SELECT m.id AS match_id, m.tx_firma
          FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = p_no_banco
           AND m.deshecho_en IS NULL
           AND m.tx_firma IS NOT NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM scintela.transacciones_bancarias t
                WHERE t.id_transaccion = m.id_transaccion
           )
    ),
    nuevo_id AS (
        SELECT d.match_id,
               (SELECT t.id_transaccion
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = p_no_banco
                   AND (COALESCE(t.fecha::TEXT, '') || '|'
                     || COALESCE(t.documento, '') || '|'
                     || COALESCE(t.importe::TEXT, '0') || '|'
                     || COALESCE(t.numreferencia::TEXT, '') || '|'
                     || COALESCE(LEFT(t.concepto, 40), '')) = d.tx_firma
                 ORDER BY t.id_transaccion ASC
                 LIMIT 1) AS new_id
          FROM dead_matches d
    )
    UPDATE scintela.banco_conciliacion_match m
       SET id_transaccion = n.new_id
      FROM nuevo_id n
     WHERE m.id = n.match_id AND n.new_id IS NOT NULL;
    GET DIAGNOSTICS v_relink = ROW_COUNT;

    -- Matches que no encontraron pareja (firma huérfana).
    SELECT COUNT(*) INTO v_sinmatch
      FROM scintela.banco_conciliacion_match m
     WHERE m.no_banco = p_no_banco
       AND m.deshecho_en IS NULL
       AND m.id_transaccion IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM scintela.transacciones_bancarias t
            WHERE t.id_transaccion = m.id_transaccion
       );

    -- Re-aplicar stat='*' a las filas re-linked.
    UPDATE scintela.transacciones_bancarias t
       SET stat = '*'
      FROM scintela.banco_conciliacion_match m
     WHERE m.no_banco = p_no_banco
       AND m.deshecho_en IS NULL
       AND m.id_transaccion = t.id_transaccion
       AND COALESCE(TRIM(t.stat), '') <> '*';

    matches_total := v_total;
    relinked := v_relink;
    sin_firma := v_sinfirma;
    sin_match := v_sinmatch;
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;
