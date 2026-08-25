-- 0216 · Los kilos abiertos por FORMA y por CALIDAD a la vez
--
-- Dueña 25/08/2026: *"idem con PRI y SEG como tubular y abierta, no es lo
-- mismo"*. La hoja que se sale a vender abre el color en una línea por cada
-- combinación que exista: tubular de primera, tubular de segunda, abierta de
-- primera, abierta de segunda. Un renglón de 171 kg que en realidad son 95
-- tubulares de segunda y 76 abiertas de primera promete cuatro cosas distintas.
--
-- Las dos columnas de la migración 0214 (kg_tubular, kg_abierta) se quedan: son
-- las que usa la pantalla, que sigue con una fila por tela × color.

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_tub_pri NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_tub_seg NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_abi_pri NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_abi_seg NUMERIC(12,2) NOT NULL DEFAULT 0;
