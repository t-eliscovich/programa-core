-- 0070: Relink usando UPDATE plain via correlated subquery.
--
-- Las versiones pl/pgsql anteriores (mig 0066/0067/0069) reportaban
-- relinked=3 pero el UPDATE no persistía. El stress test mostró que el
-- mismo patrón ejecutado como UPDATE directo via correlated subquery
-- funciona. Reescribimos la función minimalista para encapsular ese
-- UPDATE y devolver los counts.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

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

    -- UPDATE directo: para cada match huérfano (id_transaccion no existe),
    -- buscar la nueva id via firma.
    UPDATE scintela.banco_conciliacion_match m
       SET id_transaccion = (
         SELECT t.id_transaccion
           FROM scintela.transacciones_bancarias t
          WHERE t.no_banco = m.no_banco
            AND (COALESCE(t.fecha::TEXT, '') || '|'
              || COALESCE(t.documento, '') || '|'
              || COALESCE(t.importe::TEXT, '0') || '|'
              || COALESCE(t.numreferencia::TEXT, '') || '|'
              || COALESCE(LEFT(t.concepto, 40), '')) = m.tx_firma
          ORDER BY t.id_transaccion ASC LIMIT 1
       )
     WHERE m.no_banco = p_no_banco
       AND m.deshecho_en IS NULL
       AND m.tx_firma IS NOT NULL
       AND m.id_transaccion IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM scintela.transacciones_bancarias t2
            WHERE t2.id_transaccion = m.id_transaccion
       );
    GET DIAGNOSTICS v_relink = ROW_COUNT;

    -- Matches que aun no encontraron pareja (firma huérfana).
    SELECT COUNT(*) INTO v_sinmatch
      FROM scintela.banco_conciliacion_match m
     WHERE m.no_banco = p_no_banco
       AND m.deshecho_en IS NULL
       AND m.id_transaccion IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM scintela.transacciones_bancarias t
            WHERE t.id_transaccion = m.id_transaccion
       );

    -- Re-aplicar stat='*' a re-linked.
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
