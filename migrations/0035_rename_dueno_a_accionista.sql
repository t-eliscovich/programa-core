-- 0035_rename_dueno_a_accionista.sql
-- TMT 2026-05-19 v8 — pedido literal dueña: "cambiar roles de dueno a
-- accionista". El rol "Dueño" pasa a llamarse "Accionista" (es la figura
-- legal de quien controla la empresa, no "dueña" coloquial).
--
-- Idempotente: si el rol ya está renombrado, NO hace nada. Si la fila
-- "Accionista" ya existe (porque alguien corrió config/roles.py vía
-- migración 0003 primero), unifica: borra la fila "Dueño" después de
-- transferir sus usuarios al "Accionista" existente.
--
-- También cubre la variante sin tilde "Dueno" por si algún seed local
-- la creó así.

DO $$
DECLARE
    id_dueno      int;
    id_accionista int;
BEGIN
    -- Buscar el rol "Dueño" (con o sin tilde).
    SELECT id_rol INTO id_dueno
      FROM seguridad.rol
     WHERE lower(nombre_rol) IN ('dueño', 'dueno')
     LIMIT 1;

    SELECT id_rol INTO id_accionista
      FROM seguridad.rol
     WHERE lower(nombre_rol) = 'accionista'
     LIMIT 1;

    IF id_dueno IS NULL THEN
        -- Nada que renombrar. Asegurar que "Accionista" exista no es
        -- tarea de esta migración (eso lo hace 0003_seed_roles.py).
        RAISE NOTICE 'No hay rol "Dueño" — nada que renombrar.';
        RETURN;
    END IF;

    IF id_accionista IS NULL THEN
        -- Caso normal: simplemente renombrar.
        UPDATE seguridad.rol
           SET nombre_rol = 'Accionista'
         WHERE id_rol = id_dueno;
        RAISE NOTICE 'Rol "Dueño" renombrado a "Accionista".';
    ELSE
        -- Conflicto: ambas filas existen. Migrar usuarios + permisos del
        -- viejo al nuevo, después borrar el viejo.
        UPDATE seguridad.usuario
           SET id_rol = id_accionista
         WHERE id_rol = id_dueno;
        -- Los permisos del viejo se descartan: el "Accionista" ya tiene
        -- "*" por config/roles.py, no hace falta migrarlos.
        DELETE FROM seguridad.permiso WHERE id_rol = id_dueno;
        DELETE FROM seguridad.rol     WHERE id_rol = id_dueno;
        RAISE NOTICE 'Usuarios migrados de "Dueño" a "Accionista"; rol viejo eliminado.';
    END IF;
END $$;
