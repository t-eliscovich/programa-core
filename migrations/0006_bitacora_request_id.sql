-- =====================================================================
-- 0006_bitacora_request_id
-- =====================================================================
-- Agrega `request_id` a la bitácora para correlación de logs con renglones
-- de auditoría. El valor se genera en el `before_request` hook (uuid4),
-- se emite como header `X-Request-Id` en la respuesta, y se escribe acá
-- desde `auth.registrar_bitacora`.
--
-- Una sola request puede generar múltiples renglones (si una acción dispara
-- sub-escrituras), pero el request_id es estable dentro del request —
-- así `SELECT ... WHERE request_id = 'abc-…'` devuelve toda la traza.
-- =====================================================================

ALTER TABLE scintela.bitacora_acciones
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(36);

-- Index para lookups por request_id (el visor filtra por este campo).
CREATE INDEX IF NOT EXISTS idx_bitacora_request_id
    ON scintela.bitacora_acciones (request_id)
    WHERE request_id IS NOT NULL;

COMMENT ON COLUMN scintela.bitacora_acciones.request_id IS
    'UUID v4 generado en before_request. Se emite también como header X-Request-Id.';
