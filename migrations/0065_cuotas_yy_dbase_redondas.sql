-- Migration 0065: cuotas YY redondas dBase (corrige la 0063).
--
-- Contexto (TMT 2026-05-29 viernes mañana):
--   La dueña pidió "montos cerrados" del dBase original (MENU.PRG líneas
--   282-333). La migración 0063 cargó /25 (332, 1324, 3444...) que NO
--   son los redondos del dBase. Esta corrige con los valores del legacy:
--     INTERESES         300
--     A,E,C AG,EN,CMB  7700
--     SUELDOS          6000
--     ALQUILER          700
--     SS IESS          2400
--     14 DEC            300
--     13 DEC           1000
--     AB PROVISION     1300
--     SRI PROVISION    3300
--     JP JUB.PATRONAL   200
--     PROV.INCOBRABLE   400
--   Total/día: 23.600. Sumado al snapshot 639.800 = 663.400 (= total
--   dBase mañana, matchea exacto).
--
-- RT (PROV='RT', "IVA") no está en scintela.provisiones; lo maneja el
-- else branch de posdat.queries.buscar(), hardcoded a 8400 en commit
-- siguiente (también según dBase MENU.PRG L333: REPLA IMPORTE+8400).

UPDATE scintela.provisiones SET importe =  300 WHERE UPPER(TRIM(concepto)) LIKE 'INTER%';
UPDATE scintela.provisiones SET importe = 7700 WHERE UPPER(TRIM(concepto)) LIKE 'A,E,C%';
UPDATE scintela.provisiones SET importe = 6000 WHERE UPPER(TRIM(concepto)) LIKE 'SUELDOS%';
UPDATE scintela.provisiones SET importe =  700 WHERE UPPER(TRIM(concepto)) = 'ALQUILER';
UPDATE scintela.provisiones SET importe = 2400 WHERE UPPER(TRIM(concepto)) LIKE 'SS%';
UPDATE scintela.provisiones SET importe =  300 WHERE UPPER(TRIM(concepto)) LIKE '14%';
UPDATE scintela.provisiones SET importe = 1000 WHERE UPPER(TRIM(concepto)) LIKE '13%';
UPDATE scintela.provisiones SET importe = 1300 WHERE UPPER(TRIM(concepto)) LIKE 'AB%';
UPDATE scintela.provisiones SET importe = 3300 WHERE UPPER(TRIM(concepto)) LIKE 'SR%';
UPDATE scintela.provisiones SET importe =  200 WHERE UPPER(TRIM(concepto)) LIKE 'JP%';
UPDATE scintela.provisiones SET importe =  400 WHERE UPPER(TRIM(concepto)) LIKE 'PROV.INCOB%';
