-- 0262 · La foto del semáforo guarda también nombre, saldo y devueltos (13/10/2026)
--
-- Tamara: en el mail diario, "quiénes estaban en rojo y se fueron" y el total
-- en rojo contra ayer. La foto de 0261 guarda sólo el color; para contar el
-- rojo de AYER (y dejar afuera los rojos de centavos, igual que la pantalla
-- /analisis/cobranza) hace falta el saldo de ese día.
ALTER TABLE scintela.semaforo_foto ADD COLUMN IF NOT EXISTS nombre VARCHAR(80);
ALTER TABLE scintela.semaforo_foto ADD COLUMN IF NOT EXISTS saldo  NUMERIC(14,2);
ALTER TABLE scintela.semaforo_foto ADD COLUMN IF NOT EXISTS reb    NUMERIC(14,2);
