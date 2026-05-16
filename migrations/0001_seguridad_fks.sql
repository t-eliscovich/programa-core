-- =====================================================================
-- 0001_seguridad_fks
-- =====================================================================
-- El dump inicial de la fábrica trae FKs en seguridad.permiso y
-- seguridad.usuario apuntando a una tabla `rol` que NO es seguridad.rol
-- (probablemente una rol legacy). Esta migración:
--
--   1. Dropea las FKs legacy (las que NO apuntan a seguridad.rol).
--   2. Asegura PKs, UNIQUE y secuencias serial en seguridad.rol/usuario/permiso.
--   3. Si — y SOLO si — en el paso 1 efectivamente se dropeó alguna FK legacy,
--      trunca permiso/usuario/rol para limpiar data huérfana. La re-siembra
--      la hace 0003_seed_roles.py a continuación.
--   4. Recrea las FKs correctamente apuntando a seguridad.rol(id_rol), pero
--      sólo si todavía no existen (IF NOT EXISTS vía DO-block).
--
-- IDEMPOTENTE y SEGURA ante re-ejecución:
--   - Si las FKs ya apuntan a seguridad.rol (caso común después de la
--     primera aplicación), NO se dropea nada y NO se trunca nada — los
--     usuarios / roles / permisos sembrados por la app se preservan.
--   - Si alguien re-corre con --force sobre una DB ya migrada, tampoco
--     pierde data: el paso 1 no encuentra FKs legacy → dropped_count=0 →
--     no TRUNCATE.
--
-- Esta es una corrección del bug descubierto 2026-04-17 (batch 8):
-- la versión anterior truncaba incondicionalmente y borraba al admin
-- si se re-corría la migración sobre una DB con el tracker vacío.
-- =====================================================================

-- 1) Dropear SOLO las FKs que NO apuntan a seguridad.rol, y recordar
--    cuántas dropeamos. Ese contador decide si hay que truncar después.
-- 2) Asegurar PKs/UNIQUE/secuencias.
-- 3) Truncar CONDICIONALMENTE (sólo si hubo FKs legacy que dropear).
-- 4) Recrear FKs correctas si faltan.
-- Todo en un solo DO-block para poder pasar `dropped_count` entre pasos.
DO $$
DECLARE
    c record;
    dropped_count int := 0;
    rol_oid oid := 'seguridad.rol'::regclass::oid;
BEGIN
    ----------------------------------------------------------------
    -- Paso 1: drop FKs legacy (las que apuntan FUERA de seguridad.rol).
    ----------------------------------------------------------------
    FOR c IN
        SELECT conname, conrelid::regclass AS tbl
          FROM pg_constraint
         WHERE contype = 'f'
           AND conrelid IN ('seguridad.permiso'::regclass,
                            'seguridad.usuario'::regclass)
           AND confrelid <> rol_oid
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
        dropped_count := dropped_count + 1;
    END LOOP;

    RAISE NOTICE 'migración 0001: FKs legacy dropeadas = %', dropped_count;

    ----------------------------------------------------------------
    -- Paso 2a: PKs si faltan.
    ----------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'seguridad.rol'::regclass AND contype = 'p') THEN
        ALTER TABLE seguridad.rol ADD PRIMARY KEY (id_rol);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'seguridad.usuario'::regclass AND contype = 'p') THEN
        ALTER TABLE seguridad.usuario ADD PRIMARY KEY (id_usuario);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'seguridad.permiso'::regclass AND contype = 'p') THEN
        ALTER TABLE seguridad.permiso ADD PRIMARY KEY (id_permiso);
    END IF;

    ----------------------------------------------------------------
    -- Paso 2b: UNIQUE en rol.nombre_rol si falta.
    ----------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'seguridad.rol'::regclass
                      AND contype = 'u'
                      AND pg_get_constraintdef(oid) LIKE '%(nombre_rol)%') THEN
        ALTER TABLE seguridad.rol ADD CONSTRAINT rol_nombre_rol_key UNIQUE (nombre_rol);
    END IF;

    ----------------------------------------------------------------
    -- Paso 2c: Secuencias serial (solo si no tienen default).
    ----------------------------------------------------------------
    IF (SELECT pg_get_serial_sequence('seguridad.rol', 'id_rol')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.rol_id_rol_seq;
        ALTER TABLE seguridad.rol
            ALTER COLUMN id_rol SET DEFAULT nextval('seguridad.rol_id_rol_seq');
        ALTER SEQUENCE seguridad.rol_id_rol_seq OWNED BY seguridad.rol.id_rol;
        PERFORM setval('seguridad.rol_id_rol_seq',
            GREATEST(COALESCE((SELECT MAX(id_rol) FROM seguridad.rol), 0), 1), true);
    END IF;
    IF (SELECT pg_get_serial_sequence('seguridad.usuario', 'id_usuario')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.usuario_id_usuario_seq;
        ALTER TABLE seguridad.usuario
            ALTER COLUMN id_usuario SET DEFAULT nextval('seguridad.usuario_id_usuario_seq');
        ALTER SEQUENCE seguridad.usuario_id_usuario_seq OWNED BY seguridad.usuario.id_usuario;
        PERFORM setval('seguridad.usuario_id_usuario_seq',
            GREATEST(COALESCE((SELECT MAX(id_usuario) FROM seguridad.usuario), 0), 1), true);
    END IF;
    IF (SELECT pg_get_serial_sequence('seguridad.permiso', 'id_permiso')) IS NULL THEN
        CREATE SEQUENCE IF NOT EXISTS seguridad.permiso_id_permiso_seq;
        ALTER TABLE seguridad.permiso
            ALTER COLUMN id_permiso SET DEFAULT nextval('seguridad.permiso_id_permiso_seq');
        ALTER SEQUENCE seguridad.permiso_id_permiso_seq OWNED BY seguridad.permiso.id_permiso;
        PERFORM setval('seguridad.permiso_id_permiso_seq',
            GREATEST(COALESCE((SELECT MAX(id_permiso) FROM seguridad.permiso), 0), 1), true);
    END IF;

    ----------------------------------------------------------------
    -- Paso 3: TRUNCATE CONDICIONAL.
    --   Sólo truncamos si en el paso 1 se dropeó al menos una FK legacy.
    --   Esa es la señal de que acabamos de salir del dump — data con
    --   referencias rotas a la rol legacy, que 0003 va a re-sembrar limpio.
    --   Si dropped_count=0 ya estábamos bien → NO truncar (preserva admin).
    ----------------------------------------------------------------
    IF dropped_count > 0 THEN
        RAISE NOTICE 'migración 0001: truncando permiso/usuario/rol (dump legacy detectado)';
        TRUNCATE seguridad.permiso, seguridad.usuario, seguridad.rol RESTART IDENTITY CASCADE;
    ELSE
        RAISE NOTICE 'migración 0001: FKs ya OK, preservando data existente (no truncate)';
    END IF;

    ----------------------------------------------------------------
    -- Paso 4: Recrear FKs correctas si faltan.
    --   Idempotente: si ya existen apuntando a seguridad.rol, skip.
    ----------------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.permiso'::regclass
           AND contype = 'f'
           AND confrelid = rol_oid
    ) THEN
        ALTER TABLE seguridad.permiso
            ADD CONSTRAINT permiso_id_rol_fkey
            FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'seguridad.usuario'::regclass
           AND contype = 'f'
           AND confrelid = rol_oid
    ) THEN
        ALTER TABLE seguridad.usuario
            ADD CONSTRAINT usuario_id_rol_fkey
            FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol);
    END IF;
END$$;
