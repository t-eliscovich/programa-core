-- 0214 · La hoja dice si la tela es TUBULAR o ABIERTA
--
-- Dueña 25/08/2026: *"agregar si es tubular o abierta"*, en la pantalla y en la
-- lista impresa. Los kilos NO se separan —una tela × color sigue siendo una
-- sola fila, como ella misma pidió ("sumar tubular y abierta")— pero la fila
-- tiene que decir de qué forma son esos kilos: es lo primero que pregunta el
-- cliente por teléfono.
--
-- ⚠ En Asinfo la forma es un atributo del LOTE, no del producto ni del nombre
-- de la tela: `lote.id_valor_atributo_3` = 1 (TUB) o 2 (ABI). Los slots del
-- lote no siguen el número del atributo — la calidad está en el `_2` y el color
-- en el `_1`. Medido el 25/08/2026.
--
-- Una tela × color puede tener kilos de las dos formas (Franela los tiene), por
-- eso son dos columnas y no una.

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_tubular NUMERIC(12,2) NOT NULL DEFAULT 0;

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_abierta NUMERIC(12,2) NOT NULL DEFAULT 0;
