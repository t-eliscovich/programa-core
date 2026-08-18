-- 0200_competencia_cierre.sql
-- La competencia termina el 31/12/2026.
-- Dueña 17/08/2026: "es una competencia hasta fin de año".
--
-- La fecha va en `parado_config` y no hardcodeada en la plantilla porque la
-- presentación que se les da a los vendedores dice la misma fecha: si un día
-- se corre, tiene que cambiar en UN lugar y no en dos que se olvidan de
-- coincidir.

INSERT INTO scintela.parado_config (clave, valor)
VALUES ('cierre', '2026-12-31')
ON CONFLICT (clave) DO NOTHING;
