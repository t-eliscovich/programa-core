-- Migration 0122: multiplicar ×1000 los cupos de clientes.
--
-- CONTEXTO (dueña 2026-07-09): los cupos de crédito de scintela.cliente estaban
-- cargados en MILES (ej. 20 = 20.000 USD). La dueña pidió pasarlos al valor
-- REAL en $, para que en el estado de cuenta figuren como "$ 20.000" y el
-- "% usado" se calcule bien (saldo / cupo real). De acá en más los cupos se
-- cargan/editan en $ reales (ej. 20000).
--
-- Multiplica ×1000 todos los cupos no nulos / no cero. Corre UNA sola vez
-- (versionado de migraciones); no re-multiplica.
-- EXCEPTO BED: la dueña ya le cargó el cupo real (×1000) a mano desde la
-- ficha, así que se excluye para no multiplicarlo dos veces. TMT 2026-07-09.
UPDATE scintela.cliente
   SET cupo = cupo * 1000
 WHERE COALESCE(cupo, 0) <> 0
   AND UPPER(TRIM(codigo_cli)) <> 'BED';
