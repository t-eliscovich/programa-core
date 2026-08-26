-- 0230_parado_venta_numf.sql — TMT 2026-08-26
--
-- ⭐ El NÚMERO DE FACTURA de cada renglón vendido de la competencia (dueña:
-- "¿me podés poner un link a la factura también? así la veo"). Sin esto, para
-- ver de dónde salían los 85 kg de James 1.2 había que ir a /facturas y buscar
-- por fecha y cliente a mano.
--
-- ⚠ La tabla se REHACE entera en cada refresco (DELETE + INSERT), así que la
-- columna se llena sola en la próxima corrida; no hace falta backfill.
--
-- ⚠ Va como INTEGER y no como el string de Asinfo ("001-099-000182637"): en
-- Programa Core la factura se busca por `numf`, que es el número pelado
-- (182637). Verificado el 26/08/2026 contra las dos facturas del día.
ALTER TABLE scintela.parado_venta
    ADD COLUMN IF NOT EXISTS numf INTEGER;

COMMENT ON COLUMN scintela.parado_venta.numf IS
    'Número de factura de Asinfo, pelado, para linkear a /facturas?q=<numf>. '
    'Se llena en el refresco; NULL en los renglones que se guardaron antes.';
