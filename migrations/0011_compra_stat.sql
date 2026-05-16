-- =====================================================================
-- 0011_compra_stat
-- =====================================================================
-- Agrega a `scintela.compra` las columnas necesarias para soportar
-- anulación (paridad con `scintela.factura`).
--
--   - stat          char(1) — 'Y' indica anulada (mismo convenio que factura).
--                   NULL/vacío = vigente. Se deja NULL para filas históricas
--                   para que las queries legacy con `stat = 'Y'` devuelvan
--                   false naturalmente.
--   - observacion   varchar(200) — traza de anulación (motivo + timestamp).
--
-- Regla: en Ecuador una factura de compra se "anula" marcándola como tal;
-- el comprobante del SRI sigue existiendo como prueba histórica, pero
-- contablemente no cuenta. Además se borra el `posdat` asociado (si existía)
-- porque la obligación de pago desaparece junto con la anulación.
-- =====================================================================

ALTER TABLE scintela.compra
    ADD COLUMN IF NOT EXISTS stat        char(1),
    ADD COLUMN IF NOT EXISTS observacion varchar(200);

-- Index parcial sobre las anuladas — típicamente son pocas, pero queries
-- del informe de deudas / histórico necesitan filtrarlas eficientemente.
CREATE INDEX IF NOT EXISTS idx_compra_stat
    ON scintela.compra (stat)
    WHERE stat IS NOT NULL;

COMMENT ON COLUMN scintela.compra.stat IS
    '''Y'' = anulada. NULL/vacio = vigente. Paridad con scintela.factura.';

COMMENT ON COLUMN scintela.compra.observacion IS
    'Traza libre — se usa para apend [ANUL <motivo>] al anular.';
