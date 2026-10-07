-- 0259 · El ACABADO de cada venta de Saldos (07/10/2026)
--
-- La tabla de Vendidos y el detalle de la Competencia mostraban la forma del
-- STOCK de la tela (TUB en cuanto quedaba un rollo tubular), no la de lo que
-- se vendió: 117 renglones / 8.777 kg desde la largada decían la forma
-- equivocada. La venta trae su acabado en la línea de factura (o en la línea
-- madre, si es nota de crédito); el refresco lo guarda acá.
ALTER TABLE scintela.parado_venta
    ADD COLUMN IF NOT EXISTS acabado VARCHAR(5);
