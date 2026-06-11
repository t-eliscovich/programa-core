-- 0097_fantasmas_asinfo_estado0.sql
-- TMT 2026-06-11 — desactivar las 6 facturas "fantasma" de Asinfo (estado=0).
--
-- HALLAZGO (sesion 11/06): en Asinfo, fc.estado = 0 = emision NO autorizada
-- por el SRI. Esas facturas se RE-EMITEN despues con otro numero; el dBase
-- tipea solo la version corregida. PC importo LAS DOS (la card 199 de
-- Metabase incluia estado 0 en su WHERE) → doble conteo de kg fisico.
-- Las 6 (hoy usuario_crea='asinfo-backfill', $6.339,36, +713,05 kg):
--
--   N° SRI   cliente  destino real en Asinfo
--   177714    TJC     re-emitida (176534/176658/176659/176884)
--   177712    TJC     re-emitida (idem)
--   177711    TJC     re-emitida (idem)
--   177710    AFC     NUNCA autorizada (no se re-emitio)
--   177709    CTE     NUNCA autorizada (no se re-emitio)
--   177708    GED     re-emitida
--
-- NO se toca EHS 177645: esa es REAL (estado autorizado), el dBase ya la
-- tiene y el sync la aparea solo.
--
-- MECANISMO ELEGIDO: stat='X' (anulada — patron canonico: TODAS las queries
-- de kg fisico, KV, cartera/TOTF y el comparador ya excluyen stat X) +
-- usuario_crea='asinfo-fantasma' (marker nuevo que: a) el sync DBF preserva
-- — delete_where de import_dbf.py lo incluye desde este mismo commit; b) en
-- /facturas/desde-asinfo la fila cuenta como "ya cargada" → no se re-ofrece
-- el boton Cargar; c) deja rastro auditable de POR QUE se anulo).
-- Como backfill NO contaban en cartera/TOTF, esto NO mueve TOTF: solo saca
-- los +713,05 kg del calculo de stock fisico (VSTO) y del KV del comparador.
--
-- REVERSIBLE:
--   UPDATE scintela.factura
--      SET stat = 'Z', usuario_crea = 'asinfo-backfill'
--    WHERE usuario_crea = 'asinfo-fantasma';
--
-- QUIRURGICO: key por numf_completo (sufijo SRI exacto) + cliente +
-- usuario_crea actual — mismo patron que migs 0095/0096. NO toca dbf-import.
-- IDEMPOTENTE: el WHERE pide usuario_crea='asinfo-backfill'; tras correr
-- quedan en 'asinfo-fantasma' → re-correrla es no-op.
-- GUARD to_regclass OBLIGATORIO: no-op en test/CI donde scintela.factura
-- no existe (la CI se rompio una vez por omitirlo — ver mig 0094 b02ef1f).

DO $$
DECLARE
    _n integer;
BEGIN
    IF to_regclass('scintela.factura') IS NULL THEN
        RAISE NOTICE 'scintela.factura no existe (test/CI) — skip 0097';
        RETURN;
    END IF;

    UPDATE scintela.factura
       SET stat             = 'X',
           usuario_crea     = 'asinfo-fantasma',
           usuario_modifica = 'mig-0097',
           fecha_modifica   = CURRENT_TIMESTAMP
     WHERE usuario_crea = 'asinfo-backfill'
       AND (
            (numf_completo LIKE '%177714' AND TRIM(codigo_cli) = 'TJC') OR
            (numf_completo LIKE '%177712' AND TRIM(codigo_cli) = 'TJC') OR
            (numf_completo LIKE '%177711' AND TRIM(codigo_cli) = 'TJC') OR
            (numf_completo LIKE '%177710' AND TRIM(codigo_cli) = 'AFC') OR
            (numf_completo LIKE '%177709' AND TRIM(codigo_cli) = 'CTE') OR
            (numf_completo LIKE '%177708' AND TRIM(codigo_cli) = 'GED')
       );
    GET DIAGNOSTICS _n = ROW_COUNT;
    RAISE NOTICE '0097: % facturas fantasma (estado 0 Asinfo) anuladas (esperadas 6; 0 si ya corrio)', _n;
END $$;
