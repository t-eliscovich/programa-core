-- Migration 0118: renombrar el rol "Alex" → "INT"
--
-- CONTEXTO (dueña 2026-07-08): en el header de la app los usuarios con el rol
-- "Alex" (irene, maribel, el propio Alex) aparecían como "· Alex". La dueña
-- pidió que el rol se llame "INT" para que no diga "Alex" en otros usuarios.
--
-- Renombramos la FILA existente (UPDATE del nombre) — así se preserva id_rol,
-- todos los permisos (rol_permiso apunta por id_rol) y todas las asignaciones
-- de usuario (seguridad.usuario.id_rol). NO se crea un rol nuevo ni se toca
-- ningún permiso. config/roles.py ya usa "INT" para futuros seeds.
--
-- Idempotente: sólo renombra si todavía existe "Alex" y aún no existe "INT"
-- (evita chocar contra el UNIQUE(nombre_rol) si ya se corrió o si el seed ya
-- creó "INT").

UPDATE seguridad.rol AS r
   SET nombre_rol = 'INT'
 WHERE r.nombre_rol = 'Alex'
   AND NOT EXISTS (
       SELECT 1 FROM seguridad.rol r2 WHERE r2.nombre_rol = 'INT'
   );
