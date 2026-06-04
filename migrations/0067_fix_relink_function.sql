-- 0067: Re-implementar relink_matches_post_sync sin CTE.
--
-- El stress test mostró que el WITH...UPDATE devolvía ROW_COUNT=3 pero los
-- id_transaccion no cambiaban. Sospechosa interacción del CTE en pl/pgsql.
-- Reescribimos con un loop explícito que es más claro y menos misterioso.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

CREATE OR REPLACE FUNCTION scintela.relink_matches_post_sync(p_no_banco INTEGER)
RETURNS TABLE(matches_total INTEGER, relinked INTEGER, sin_firma INTEGER, sin_match INTEGER) AS $$
DECLARE
    r RECORD;
    v_new_id INTEGER;
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

    -- Loop sobre matches con id_transaccion huérfano y firma conocida.
    FOR r IN
        SELECT m.id, m.tx_firma
          FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = p_no_banco
           AND m.deshecho_en IS NULL
           AND m.tx_firma IS NOT NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM scintela.transacciones_bancarias t
                WHERE t.id_transaccion = m.id_transaccion
           )
    LOOP
        SELECT t.id_transaccion INTO v_new_id
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = p_no_banco
           AND (COALESCE(t.fecha::TEXT, '') || '|'
             || COALESCE(t.documento, '') || '|'
             || COALESCE(t.importe::TEXT, '0') || '|'
             || COALESCE(t.numreferencia::TEXT, '') || '|'
             || COALESCE(LEFT(t.concepto, 40), '')) = r.tx_firma
         ORDER BY t.id_transaccion ASC
         LIMIT 1;

        IF v_new_id IS NOT NULL THEN
            UPDATE scintela.banco_conciliacion_match
               SET id_transaccion = v_new_id
             WHERE id = r.id;
            v_relink := v_relink + 1;
        END IF;
    END LOOP;

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
