-- 0069: Relink rewrite con aliases explicitos para evitar ambigüedad
-- entre nombres de columna (id, no_banco, tx_firma) y record fields.
-- El stress test mostraba relinked=3 pero id_transaccion no cambiaba —
-- sospecho que WHERE id = r.id estaba bound al columna en vez del record.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

CREATE OR REPLACE FUNCTION scintela.relink_matches_post_sync(p_no_banco INTEGER)
RETURNS TABLE(matches_total INTEGER, relinked INTEGER, sin_firma INTEGER, sin_match INTEGER) AS $$
DECLARE
    rec_id BIGINT;
    rec_firma TEXT;
    v_new_id INTEGER;
    v_total INTEGER;
    v_relink INTEGER := 0;
    v_sinfirma INTEGER;
    v_sinmatch INTEGER := 0;
    cur_dead REFCURSOR;
BEGIN
    SELECT COUNT(*) INTO v_total
      FROM scintela.banco_conciliacion_match
     WHERE no_banco = p_no_banco AND deshecho_en IS NULL;

    SELECT COUNT(*) INTO v_sinfirma
      FROM scintela.banco_conciliacion_match
     WHERE no_banco = p_no_banco AND deshecho_en IS NULL
       AND tx_firma IS NULL AND id_transaccion IS NOT NULL;

    OPEN cur_dead FOR
        SELECT m.id, m.tx_firma
          FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = p_no_banco
           AND m.deshecho_en IS NULL
           AND m.tx_firma IS NOT NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM scintela.transacciones_bancarias tt
                WHERE tt.id_transaccion = m.id_transaccion
           );

    LOOP
        FETCH cur_dead INTO rec_id, rec_firma;
        EXIT WHEN NOT FOUND;

        v_new_id := NULL;
        SELECT tt.id_transaccion INTO v_new_id
          FROM scintela.transacciones_bancarias tt
         WHERE tt.no_banco = p_no_banco
           AND (COALESCE(tt.fecha::TEXT, '') || '|'
             || COALESCE(tt.documento, '') || '|'
             || COALESCE(tt.importe::TEXT, '0') || '|'
             || COALESCE(tt.numreferencia::TEXT, '') || '|'
             || COALESCE(LEFT(tt.concepto, 40), '')) = rec_firma
         ORDER BY tt.id_transaccion ASC
         LIMIT 1;

        IF v_new_id IS NOT NULL THEN
            UPDATE scintela.banco_conciliacion_match AS mm
               SET id_transaccion = v_new_id
             WHERE mm.id = rec_id;
            v_relink := v_relink + 1;
        END IF;
    END LOOP;

    CLOSE cur_dead;

    -- Matches que no encontraron pareja.
    SELECT COUNT(*) INTO v_sinmatch
      FROM scintela.banco_conciliacion_match m
     WHERE m.no_banco = p_no_banco
       AND m.deshecho_en IS NULL
       AND m.id_transaccion IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM scintela.transacciones_bancarias tt
            WHERE tt.id_transaccion = m.id_transaccion
       );

    -- Re-aplicar stat='*'.
    UPDATE scintela.transacciones_bancarias tb
       SET stat = '*'
      FROM scintela.banco_conciliacion_match mm
     WHERE mm.no_banco = p_no_banco
       AND mm.deshecho_en IS NULL
       AND mm.id_transaccion = tb.id_transaccion
       AND COALESCE(TRIM(tb.stat), '') <> '*';

    matches_total := v_total;
    relinked := v_relink;
    sin_firma := v_sinfirma;
    sin_match := v_sinmatch;
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;
