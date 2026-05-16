-- =====================================================================
-- Programa Core — arreglar FKs en seguridad.*
-- =====================================================================
-- El dump trae `seguridad.permiso.id_rol` y `seguridad.usuario.id_rol`
-- con FKs que apuntan a una tabla `rol` que NO es `seguridad.rol`
-- (probablemente una rol legacy del dump). Por eso inserts válidos en
-- seguridad.rol fallan con "not present in table rol".
--
-- Este script:
--   1) dropea toda FK a tablas rol/usuario ajenas a seguridad.*
--   2) asegura PK en seguridad.rol y seguridad.usuario
--   3) recrea FKs correctamente apuntando a seguridad.*
-- =====================================================================

BEGIN;

-- 1) Eliminar TODAS las FKs existentes sobre id_rol en seguridad.permiso y seguridad.usuario
--    (cualquier nombre, cualquier destino)
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT conname, conrelid::regclass AS tbl
          FROM pg_constraint
         WHERE contype = 'f'
           AND conrelid IN ('seguridad.permiso'::regclass, 'seguridad.usuario'::regclass)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
        RAISE NOTICE 'dropped FK % on %', c.conname, c.tbl;
    END LOOP;
END$$;

-- 2) Asegurar PKs / UNIQUE en seguridad.rol (id_rol) y seguridad.usuario (id_usuario)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.rol'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE seguridad.rol ADD PRIMARY KEY (id_rol);
        RAISE NOTICE 'added PK on seguridad.rol(id_rol)';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.usuario'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE seguridad.usuario ADD PRIMARY KEY (id_usuario);
        RAISE NOTICE 'added PK on seguridad.usuario(id_usuario)';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.permiso'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE seguridad.permiso ADD PRIMARY KEY (id_permiso);
        RAISE NOTICE 'added PK on seguridad.permiso(id_permiso)';
    END IF;
END$$;

-- 3) Recrear FKs correctamente
ALTER TABLE seguridad.permiso
    ADD CONSTRAINT permiso_id_rol_fkey
    FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol) ON DELETE CASCADE;

ALTER TABLE seguridad.usuario
    ADD CONSTRAINT usuario_id_rol_fkey
    FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol);

-- 4) Asegurar que id_rol en seguridad.rol tenga secuencia (si no la tiene)
DO $$
BEGIN
    IF (SELECT pg_get_serial_sequence('seguridad.rol', 'id_rol')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.rol_id_rol_seq;
        ALTER TABLE seguridad.rol
            ALTER COLUMN id_rol SET DEFAULT nextval('seguridad.rol_id_rol_seq');
        ALTER SEQUENCE seguridad.rol_id_rol_seq OWNED BY seguridad.rol.id_rol;
        PERFORM setval('seguridad.rol_id_rol_seq',
                       GREATEST(COALESCE((SELECT MAX(id_rol) FROM seguridad.rol), 0), 1),
                       true);
        RAISE NOTICE 'added sequence on seguridad.rol(id_rol)';
    END IF;

    IF (SELECT pg_get_serial_sequence('seguridad.usuario', 'id_usuario')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.usuario_id_usuario_seq;
        ALTER TABLE seguridad.usuario
            ALTER COLUMN id_usuario SET DEFAULT nextval('seguridad.usuario_id_usuario_seq');
        ALTER SEQUENCE seguridad.usuario_id_usuario_seq OWNED BY seguridad.usuario.id_usuario;
        PERFORM setval('seguridad.usuario_id_usuario_seq',
                       GREATEST(COALESCE((SELECT MAX(id_usuario) FROM seguridad.usuario), 0), 1),
                       true);
        RAISE NOTICE 'added sequence on seguridad.usuario(id_usuario)';
    END IF;

    IF (SELECT pg_get_serial_sequence('seguridad.permiso', 'id_permiso')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.permiso_id_permiso_seq;
        ALTER TABLE seguridad.permiso
            ALTER COLUMN id_permiso SET DEFAULT nextval('seguridad.permiso_id_permiso_seq');
        ALTER SEQUENCE seguridad.permiso_id_permiso_seq OWNED BY seguridad.permiso.id_permiso;
        PERFORM setval('seguridad.permiso_id_permiso_seq',
                       GREATEST(COALESCE((SELECT MAX(id_permiso) FROM seguridad.permiso), 0), 1),
                       true);
        RAISE NOTICE 'added sequence on seguridad.permiso(id_permiso)';
    END IF;
END$$;

-- 5) UNIQUE en rol.nombre_rol (si falta) — lo usa seed_roles.py
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.rol'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) LIKE '%(nombre_rol)%'
    ) THEN
        ALTER TABLE seguridad.rol ADD CONSTRAINT rol_nombre_rol_key UNIQUE (nombre_rol);
        RAISE NOTICE 'added UNIQUE on seguridad.rol(nombre_rol)';
    END IF;
END$$;

COMMIT;

-- Verificación
SELECT 'OK' AS estado, count(*) AS roles FROM seguridad.rol;
