-- 0201_competencia_largada_25.sql
-- La competencia arranca el MARTES 25/08/2026, no el 17.
-- Dueña 18/08/2026: "la vamos a empezar el martes que viene".
--
-- ⚠ Es un UPDATE y no un INSERT: la 0198 ya dejó la fila con el 17/08, así que
-- un `ON CONFLICT DO NOTHING` no habría cambiado nada y la pantalla habría
-- seguido contando kilos de una semana antes de que nadie supiera que existía
-- la competencia.

UPDATE scintela.parado_config
   SET valor = '2026-08-25', actualizado = NOW()
 WHERE clave = 'largada';

INSERT INTO scintela.parado_config (clave, valor)
VALUES ('largada', '2026-08-25')
ON CONFLICT (clave) DO NOTHING;
