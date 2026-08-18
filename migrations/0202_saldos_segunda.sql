-- 0202_saldos_segunda.sql
-- Entra a la competencia TODA la tela de segunda, no sólo la que está parada.
-- Dueña 18/08/2026: "agreguemos toda la tela de segunda a la competencia, y
-- re calcula".
--
-- ⭐ De un ítem que NO está parado pero tiene segunda entran SÓLO los kilos de
-- segunda. Esos mismos ítems tienen 61.272 kg de primera que se venden solos:
-- sumarlos habría inflado la competencia con tela que ya sale, y la meta
-- dejaría de significar algo. La columna `motivo` guarda por qué entró cada
-- fila — sin ella, en la pantalla no hay forma de distinguir 300 kg parados de
-- 300 kg de segunda de una tela que se vende todas las semanas.
--
-- Medido: 344 parados (36.720 kg) + 363 con segunda suelta (15.709 kg)
--       = 707 ítems y 52.428 kg. La meta por vendedor pasa de 5.285 a 7.490.

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS motivo VARCHAR(10);

CREATE INDEX IF NOT EXISTS idx_parado_foto_motivo
    ON scintela.parado_foto (motivo);
