-- 0236 · INT puede "Ver como" los VENDEDORES
--
-- Dueña 27/08/2026: *"¿Podemos autorizar a INT a ver como los vendedores?"*.
--
-- Permiso nuevo `vendedores.ver_como` para el rol INT:
--   - abre la pantalla /usuarios/vendedores (INT no tiene `usuarios.admin`,
--     así que el botón de /usuarios no le servía), y
--   - habilita /impersonate, pero SOLO sobre usuarios que son vendedores
--     (tienen `vend` cargado). El límite está en el gate de auth.py: verse
--     como un vendedor nunca escala privilegios (tiene menos que INT);
--     verse como un Accionista sí, y sigue prohibido.
--
-- Accionista no necesita nada de esto: impersona sin límite desde mayo.
--
-- El espejo en código está en config/roles.py (mismo commit) — los permisos
-- VIVOS son estas filas; el archivo es el seed y lo vigila el drift-check.
--

INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
SELECT r.id_rol, 'vendedores.ver_como'
  FROM seguridad.rol r
 WHERE r.nombre_rol = 'INT'
   AND NOT EXISTS (
         SELECT 1
           FROM seguridad.permiso p
          WHERE p.id_rol = r.id_rol
            AND p.nombre_opcion = 'vendedores.ver_como'
       );
