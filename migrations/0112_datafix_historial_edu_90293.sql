-- 0112_datafix_historial_edu_90293.sql
-- TMT 2026-07-06 (caso EDU/Alex): el cheque id 90293 (N° 51, EDU, $20.000)
-- fue anulado por error-de-carga ANTES del fix 134896e, que ahora sí marca
-- el historial y borra chequesxfact. Quedaron sucios:
--   · mov_doble #9973 (cheque_creado) y #9974 (cheque_aplicado_a_factura)
--     en estado 'activo' con boton reversar (footgun: re-reversar restaria
--     el abono de la factura 250440 OTRA VEZ).
--   · la fila de scintela.chequesxfact del cheque 90293 (1.841,56) que el
--     anular viejo no borraba.
-- Guards: solo actua si el cheque 90293 esta efectivamente en stat X.
-- Idempotente.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM scintela.cheque
                WHERE id_cheque = 90293 AND UPPER(COALESCE(stat,'')) = 'X') THEN
        UPDATE scintela.mov_doble
           SET estado = 'reversado'
         WHERE id_mov_doble IN (9973, 9974)
           AND estado = 'activo'
           AND origen_id = 90293;
        DELETE FROM scintela.chequesxfact
         WHERE id_cheque = 90293;
    END IF;
END $$;
