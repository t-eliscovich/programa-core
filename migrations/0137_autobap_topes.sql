-- 0137 — Los topes del automático: US$60.000 era cualquier cosa (TMT 2026-07-29).
--
-- Dueña: "no pongas un tope de 60k, es cualquier cosa eso... si todas son más".
-- Tiene razón: una importación entera ronda los $70-100k por sí sola (AC 18
-- cerró en $66.434,40; AC 94 en $99.519,84; AC 29 en $73.359,95). Con el tope
-- en 60k el automático frenaba SIEMPRE — o sea, no automatizaba nada.
--
-- El tope no está para gatear la operación normal: está para que un dato raro
-- de Asinfo no dispare una conversión masiva. Los nuevos valores se dimensionan
-- sobre lo real: 10 importaciones en una corrida es mucho más de lo que llega
-- en un día, y US$500.000 son ~5 importaciones completas juntas.
--
-- Y para no volver a inventar un número: ahora los dos topes se editan desde
-- la propia pantalla (/importaciones/automatico), sin deploy.

ALTER TABLE scintela.autobap_config
    ALTER COLUMN tope_importaciones SET DEFAULT 10;

ALTER TABLE scintela.autobap_config
    ALTER COLUMN tope_usd SET DEFAULT 500000;

-- Sólo se pisan los valores si siguen siendo los que sembró la 0136 (60k / 5):
-- si alguien ya los ajustó a mano desde la pantalla, se respetan.
UPDATE scintela.autobap_config
   SET tope_importaciones = 10,
       tope_usd           = 500000,
       usuario_modifica   = 'migracion-0137',
       fecha_modifica     = now()
 WHERE id = 1
   AND tope_usd = 60000
   AND tope_importaciones = 5;
