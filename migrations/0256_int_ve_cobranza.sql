-- 0256 · INT ve el análisis de cobranza (/analisis/cobranza)
--
-- Dueña 30/09/2026: *"habilita la cobranza a rol intela"*.
--
-- Permiso nuevo `analisis.cobranza` para el rol INT. Es propio (no cuelga de
-- `analisis.ver`) para que Gerente no la reciba sin que nadie lo pida.
-- Accionista y Administrador entran por `*`.
--
-- El espejo en código está en config/roles.py (mismo commit) — los permisos
-- VIVOS son estas filas; el archivo es el seed y lo vigila el drift-check.
--

INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
SELECT r.id_rol, 'analisis.cobranza'
  FROM seguridad.rol r
 WHERE r.nombre_rol = 'INT'
   AND NOT EXISTS (
         SELECT 1
           FROM seguridad.permiso p
          WHERE p.id_rol = r.id_rol
            AND p.nombre_opcion = 'analisis.cobranza'
       );
