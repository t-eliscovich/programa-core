-- =====================================================================
-- 0247 · Qué renglones de formulas armaron cada compra del puente
-- =====================================================================
-- Dueña 10/09/2026: "tienen que tener id único — ¿qué pasa si se
-- confundieron de producto y editan la orden?". Hasta acá el puente
-- reconocía la compra de PC por el N° de factura que quedó en el
-- concepto: si en formulas le cambiaban el número, para el puente era otra
-- factura (el 09/09 una importación de 273 k quedó DOS veces), y si la
-- borraban no había forma exacta de saber cuál era.
--
-- Cada renglón de compra de formulas (`public.compras.id` en la base
-- postgres del mismo RDS) tiene su id. Acá se guarda, por compra de PC,
-- qué ids la armaron. Con eso el puente reconoce la compra aunque le
-- cambien el número, la fecha o el producto, y "desapareció" pasa a ser
-- exacto: sus ids ya no están en formulas.
--
-- Un renglón de formulas pertenece a UNA compra de PC (clave primaria).
-- Las compras cargadas antes de esta migración no tienen vínculo: el
-- puente las sigue reconociendo por número y, en cuanto las matchea, les
-- guarda los ids (se completa solo).
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.compra_formulas (
    formulas_id   INTEGER PRIMARY KEY,
    id_compra     INTEGER NOT NULL REFERENCES scintela.compra(id_compra) ON DELETE CASCADE,
    vinculado_en  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS compra_formulas_id_compra_idx
    ON scintela.compra_formulas (id_compra);

COMMENT ON TABLE scintela.compra_formulas IS
    'Renglones de compras del programa de tintorería (formulas, public.compras.id) que armaron cada compra del puente. Un renglón pertenece a una sola compra.';
