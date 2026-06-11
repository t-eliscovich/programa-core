-- 0098_fantasmas_asinfo_estado0_fix_clave.sql
-- TMT 2026-06-11 — fix de la 0097: apareo por la clave EQUIVOCADA.
--
-- La 0097 filtraba numf_completo LIKE '%177714' etc., pero en estas filas
-- numf_completo guarda el N° SRI REAL de Asinfo (001-099-000176532...) y
-- 177714 es el numf interno → ROW_COUNT 0, no anulo nada.
-- Clave correcta: numf interno + cliente + marker backfill, con el sufijo
-- SRI real como verificacion. Mismo mecanismo y reversa que la 0097.
-- (Reversible: SET stat='Z', usuario_crea='asinfo-backfill'
--  WHERE usuario_crea='asinfo-fantasma')

DO $$
DECLARE
    _n integer;
BEGIN
    IF to_regclass('scintela.factura') IS NULL THEN
        RAISE NOTICE 'scintela.factura no existe (test/CI) — skip 0098';
        RETURN;
    END IF;

    UPDATE scintela.factura
       SET stat             = 'X',
           usuario_crea     = 'asinfo-fantasma',
           usuario_modifica = 'mig-0098',
           fecha_modifica   = CURRENT_TIMESTAMP
     WHERE usuario_crea = 'asinfo-backfill'
       AND (
            (numf = 177714 AND TRIM(codigo_cli) = 'TJC' AND numf_completo LIKE '%176532') OR
            (numf = 177712 AND TRIM(codigo_cli) = 'TJC' AND numf_completo LIKE '%176655') OR
            (numf = 177711 AND TRIM(codigo_cli) = 'TJC' AND numf_completo LIKE '%176656') OR
            (numf = 177710 AND TRIM(codigo_cli) = 'AFC' AND numf_completo LIKE '%176800') OR
            (numf = 177709 AND TRIM(codigo_cli) = 'CTE' AND numf_completo LIKE '%176817') OR
            (numf = 177708 AND TRIM(codigo_cli) = 'GED' AND numf_completo LIKE '%176883')
       );
    GET DIAGNOSTICS _n = ROW_COUNT;
    RAISE NOTICE '0098: % fantasmas anulados (esperadas 6; 0 si ya corrio)', _n;
END $$;
