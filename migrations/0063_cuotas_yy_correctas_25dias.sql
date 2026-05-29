-- Migration 0063: cargar las cuotas diarias YY correctas (Importe / 25 días).
--
-- Contexto (TMT 2026-05-28 noche, sesión replanear):
--   Las cuotas en scintela.provisiones quedaron descalibradas (algunas
--   eran 12000, 14000, 5000 — semánticamente "mensuales mal-rotulados
--   como diarios"). La dueña replanteó: el divisor del mes son 25 días
--   hábiles, y el importe acumulado actual / 25 da la cuota diaria
--   correcta para cada concepto.
--
-- Cuotas calculadas con `importe_actual / 25`:
--   INTERESES   8.300 / 25 =  332
--   A,E,C      33.100 / 25 = 1.324
--   SUELDOS    86.100 / 25 = 3.444
--   ALQUILER    8.500 / 25 =  340
--   SS IESS     7.300 / 25 =  292
--   14 DEC     32.300 / 25 = 1.292
--   13 DEC     65.100 / 25 = 2.604
--   AB        114.200 / 25 = 4.568
--   SRI        95.700 / 25 = 3.828
--   JP JUB     33.200 / 25 = 1.328
--   PROV.INC  156.000 / 25 = 6.240
-- Total/día: 25.592 → × 25 días = 639.800 = total YY actual (cuadra).
--
-- Match a provisiones por starts-with del concepto (case-insensitive).
-- Idempotente: si se vuelve a correr, sobreescribe con el mismo valor.

UPDATE scintela.provisiones SET importe =  332 WHERE UPPER(TRIM(concepto)) LIKE 'INTER%';
UPDATE scintela.provisiones SET importe = 1324 WHERE UPPER(TRIM(concepto)) LIKE 'A,E,C%';
UPDATE scintela.provisiones SET importe = 3444 WHERE UPPER(TRIM(concepto)) LIKE 'SUELDOS%';
UPDATE scintela.provisiones SET importe =  340 WHERE UPPER(TRIM(concepto)) = 'ALQUILER';
UPDATE scintela.provisiones SET importe =  292 WHERE UPPER(TRIM(concepto)) LIKE 'SS%';
UPDATE scintela.provisiones SET importe = 1292 WHERE UPPER(TRIM(concepto)) LIKE '14%';
UPDATE scintela.provisiones SET importe = 2604 WHERE UPPER(TRIM(concepto)) LIKE '13%';
UPDATE scintela.provisiones SET importe = 4568 WHERE UPPER(TRIM(concepto)) LIKE 'AB%';
UPDATE scintela.provisiones SET importe = 3828 WHERE UPPER(TRIM(concepto)) LIKE 'SR%';
UPDATE scintela.provisiones SET importe = 1328 WHERE UPPER(TRIM(concepto)) LIKE 'JP%';
UPDATE scintela.provisiones SET importe = 6240 WHERE UPPER(TRIM(concepto)) LIKE 'PROV.INCOB%';
