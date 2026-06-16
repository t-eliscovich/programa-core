-- 0103_accionista_wildcard.sql
-- TMT 2026-06-16 (dueña): "Accionista tiene que poder todo".
-- config/roles.py ya define Accionista = ["*"] (acceso total), pero el seed
-- 0003 no se re-corre solo cuando cambia la config. Esta migración asegura
-- que la DB LIVE coincida: deja al rol Accionista con un único permiso '*'
-- (wildcard = todo). Idempotente y re-corrible. Solo toca el rol Accionista;
-- no modifica ningún otro rol.
DO $$
BEGIN
    IF to_regclass('seguridad.rol') IS NOT NULL
       AND to_regclass('seguridad.permiso') IS NOT NULL THEN
        -- Limpiar permisos previos del rol y dejar exclusivamente '*'.
        DELETE FROM seguridad.permiso
         WHERE id_rol IN (
             SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Accionista'
         );
        INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
            SELECT id_rol, '*'
              FROM seguridad.rol
             WHERE nombre_rol = 'Accionista'
        ON CONFLICT (id_rol, nombre_opcion) DO NOTHING;
    END IF;
END $$;
