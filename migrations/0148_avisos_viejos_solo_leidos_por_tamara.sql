-- 0148 — Los avisos que ya estaban leídos pasan a estar leídos SÓLO por tamara.
--
-- TMT 2026-07-31, decisión de la dueña al estrenar la 0146: *"que les aparezcan
-- a ellos"*. La 0146 dejó la columna vieja `aviso.leido` como línea de base
-- ("leído por todos") para no hacer reaparecer el historial de golpe. Pero acá
-- el historial son 11 avisos de dos días, y están marcados leídos justamente
-- por el bug que la 0146 arregla: los abrió UNA persona y se apagaron para las
-- cuatro. Federico, Andrés y Alex nunca los vieron.
--
-- Entonces: se le anota el "leído" a tamara (que es quien los abrió) y se
-- limpia el flag global. Resultado: la campanita de tamara sigue en cero y a
-- los otros tres les aparecen los 11 que se habían perdido.
--
-- Es un ONE-OFF del corte, no una regla: de acá en adelante `aviso.leido` no se
-- escribe más y todo el "leído" vive en `aviso_leido`.

INSERT INTO scintela.aviso_leido (id_aviso, usuario)
SELECT id_aviso, 'tamara' FROM scintela.aviso WHERE leido
ON CONFLICT (id_aviso, usuario) DO NOTHING;

UPDATE scintela.aviso SET leido = FALSE WHERE leido;
