-- Agrega columna `doc_banco` a scintela.cheque.
-- TMT 2026-05-26 (dueña): "agregar en cobranza documento del banco" — campo
-- libre para registrar el N° de comprobante / depósito / transferencia /
-- referencia del banco asociado al cheque (cuando se deposita o transfiere).
--
-- Idempotente: IF NOT EXISTS.
ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS doc_banco TEXT;

COMMENT ON COLUMN scintela.cheque.doc_banco IS
    'Documento del banco — N° de comprobante / depósito / transferencia '
    'asociado al cheque. Campo libre. Agregado 2026-05-26.';
