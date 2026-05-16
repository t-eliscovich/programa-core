-- =====================================================================
-- 0014_cheque_postergacion
-- =====================================================================
-- Tracking de postergaciones de cheques. Pedido del dueño 2026-04-29:
-- "cuando el cheque es postergado falta fecha de postergación".
--
-- Hasta hoy `postergar()` actualizaba `fechad` (la nueva fecha a depositar)
-- pero perdía dos cosas:
--   - cuándo se hizo la postergación (la decisión, no la nueva fecha)
--   - cuál era la fechad original antes de postergar
--
-- Con esto la pantalla de detalle del cheque puede mostrar:
--   "Postergado el 28/04/2026 al 15/05/2026 · original 30/04/2026"
--
-- Idempotente: ADD COLUMN IF NOT EXISTS, y la query de postergar() usa
-- COALESCE(fechad_original, fechad) para no pisar la fecha original en
-- postergaciones subsecuentes (Z → P → P → P snapshot la PRIMERA fechad).
-- =====================================================================

ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS fecha_postergacion date,
    ADD COLUMN IF NOT EXISTS fechad_original    date;

CREATE INDEX IF NOT EXISTS idx_cheque_fecha_postergacion
    ON scintela.cheque (fecha_postergacion)
    WHERE fecha_postergacion IS NOT NULL;

COMMENT ON COLUMN scintela.cheque.fecha_postergacion IS
    'Fecha en que se postergó por última vez (=CURRENT_DATE al momento de '
    'postergar). NULL = nunca se postergó.';

COMMENT ON COLUMN scintela.cheque.fechad_original IS
    'fechad original antes de cualquier postergación. Sólo se setea en la '
    'primera postergación (vía COALESCE), no se pisa en subsecuentes. '
    'NULL = nunca se postergó (la fechad actual es la única que hubo).';
