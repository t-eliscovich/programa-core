-- 0264 · Qué telas de Asinfo usan cada ficha técnica (13/10/2026)
--
-- Tamara: "fijate si podés linkear todas". El 📄 de Stock cruzaba la tela de
-- Asinfo con la ficha sólo por el NOMBRE, y así se linkeaban 28 de 113: Asinfo
-- dice "Jersey 3" y la ficha "JERSEY 3.00", "Rib Normal" y "RIBB NORMAL",
-- "WAFFER" y "WAFER". Esta columna guarda, por ficha, los nombres de Asinfo
-- que la usan. Una tela puede tener DOS fichas (la tubular y la abierta): el
-- 📄 abre las dos para elegir.
--
-- Se carga acá la lista que revisó Tamara; después se edita desde
-- /admin/fichas-tecnicas ("Telas de Asinfo"), nunca por SQL.

ALTER TABLE scintela.ficha_tecnica
    ADD COLUMN IF NOT EXISTS telas_asinfo TEXT[] NOT NULL DEFAULT '{}';

UPDATE scintela.ficha_tecnica f
   SET telas_asinfo = v.telas
  FROM (VALUES
    ('FT-FL-SINPERCHAR',          ARRAY['Fleece 96 Sin Perchar']),
    ('FT-FL-SINPERCHARABIERTO',   ARRAY['Fleece 96 Sin Perchar']),
    ('FT-FL-2.20',                ARRAY['Fleece 2.2']),
    ('FT-FL-2.20ABIERTO',         ARRAY['Fleece 2.2']),
    ('FT-FL-200',                 ARRAY['FLEECE 200']),
    ('FT-FL-200ABIERTO',          ARRAY['FLEECE 200']),
    ('FT-FL-LICRA',               ARRAY['Fleece Lycra']),
    ('FT-JE-3.00',                ARRAY['Jersey 3']),
    ('FT-JE-3.50',                ARRAY['Jersey 3.5']),
    ('FT-JE-3.50ABIERTO',         ARRAY['Jersey 3.5']),
    ('FT-JE-2.60',                ARRAY['Jersey 2.6']),
    ('FT-JE-3.20',                ARRAY['Jersey 3.2']),
    ('FT-JE-3.80',                ARRAY['Jersey 3.8']),
    ('FT-JE-4.20',                ARRAY['Jersey 4.2']),
    ('FT-JE-ALGODON120',          ARRAY['Jersey Algodon 1.2']),
    ('FT-JE-LICRA200',            ARRAY['Jersey Lycra 200']),
    ('FT-JE-LICRATANIA',          ARRAY['Jersey Lycra Tania']),
    ('FT-JE-SPUN120',             ARRAY['Jersey Spun 1.2']),
    ('FT-RB-ACANALADO',           ARRAY['Rib Acanalado']),
    ('FT-RB-ACANALADOABIERTO',    ARRAY['Rib Acanalado']),
    ('FT-RB-NOR',                 ARRAY['Rib Normal']),
    ('FT-RB-NORMALABIERTO',       ARRAY['Rib Normal']),
    ('FT-RB-POLIESTER',           ARRAY['Rib Poliester']),
    ('FT-RB-SPUN',                ARRAY['Rib Spun']),
    ('FT-RB-ALGODON',             ARRAY['Rib Algodon']),
    ('FT-RB-DOBLECABOLICRA',      ARRAY['Rib Doble Cabo Lycra']),
    ('FT-RB-ACANALADOESPECIAL',   ARRAY['Rib Acanalado Especial']),
    ('FT-ROXI',                   ARRAY['Roxy']),
    ('FT-TP',                     ARRAY['Toper']),
    ('FT-TP-1.70',                ARRAY['Toper 1.7']),
    ('FT-WAFER',                  ARRAY['WAFFER']),
    ('FT-ALEMANIA',               ARRAY['Alemania']),
    ('FT-ALEMANIAABIERTA',        ARRAY['Alemania']),
    ('FT-ALEMANIA120',            ARRAY['Alemania 1.2']),
    ('FT-MICROFIBRA120',          ARRAY['Microfibra 1.2']),
    ('FT-KIANA4.15',              ARRAY['Kiana 415x90']),
    ('FT-ASTURIAS',               ARRAY['Asturias']),
    ('FT-ASTURIASABIERTA',        ARRAY['Asturias'])
  ) AS v(codigo, telas)
 WHERE f.codigo = v.codigo
   AND f.telas_asinfo = '{}';
