-- 0068: Borrar el trigger compute_tx_firma. Estaba pisando el UPDATE del
-- relink (test mostraba relinked=3 pero id_transaccion no cambiaba).
-- La firma se va a poblar directamente en Python al crear el match.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

DROP TRIGGER IF EXISTS tg_bcm_compute_tx_firma ON scintela.banco_conciliacion_match;
DROP FUNCTION IF EXISTS scintela.compute_tx_firma_for_match();

-- Helper SQL function: usable desde Python o desde INSERT statements
-- para poblar tx_firma sin trigger.
CREATE OR REPLACE FUNCTION scintela.compute_tx_firma(p_id INTEGER)
RETURNS TEXT AS $$
    SELECT COALESCE(fecha::TEXT, '') || '|'
        || COALESCE(documento, '') || '|'
        || COALESCE(importe::TEXT, '0') || '|'
        || COALESCE(numreferencia::TEXT, '') || '|'
        || COALESCE(LEFT(concepto, 40), '')
      FROM scintela.transacciones_bancarias
     WHERE id_transaccion = p_id;
$$ LANGUAGE sql STABLE;
