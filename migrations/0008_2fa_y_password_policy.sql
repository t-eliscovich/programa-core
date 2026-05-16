-- =====================================================================
-- 0008_2fa_y_password_policy
-- =====================================================================
-- Agrega columnas para:
--   - 2FA opt-in con TOTP (pyotp): totp_secret + totp_confirmado_en
--   - Password policy: password_cambio_en (para forzar rotación periódica)
--     y password_debe_cambiar (true para usuarios creados por admin —
--     la primera sesión los obliga a cambiar).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS.
-- =====================================================================

ALTER TABLE seguridad.usuario
    ADD COLUMN IF NOT EXISTS totp_secret        varchar(64),
    ADD COLUMN IF NOT EXISTS totp_confirmado_en timestamp,
    ADD COLUMN IF NOT EXISTS password_cambio_en timestamp,
    ADD COLUMN IF NOT EXISTS password_debe_cambiar boolean NOT NULL DEFAULT FALSE;

-- Backfill: los usuarios existentes ya cambiaron su password en algún
-- momento, marcamos password_cambio_en = fecha_crea para que el reloj
-- de expiración arranque de un valor sensato (no de NULL que sería
-- "cambiado nunca" y forzaría a todos a cambiar ya).
--
-- Robusto contra DBs que no tienen `fecha_crea` en seguridad.usuario
-- (algunos dumps viejos no traían audit columns en este schema). Si
-- no existe, se usa CURRENT_TIMESTAMP — mismo efecto práctico:
-- arranca el reloj HOY en vez de "nunca".
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'seguridad'
          AND table_name   = 'usuario'
          AND column_name  = 'fecha_crea'
    ) THEN
        -- EXECUTE deferred al runtime para que el parser no se queje
        -- si alguna vez se llega acá en una DB sin la columna.
        EXECUTE 'UPDATE seguridad.usuario
                    SET password_cambio_en = fecha_crea
                  WHERE password_cambio_en IS NULL';
    ELSE
        UPDATE seguridad.usuario
           SET password_cambio_en = CURRENT_TIMESTAMP
         WHERE password_cambio_en IS NULL;
    END IF;
END $$;

-- Comentarios para que el próximo que lea el schema entienda por qué
-- existen estas columnas.
COMMENT ON COLUMN seguridad.usuario.totp_secret IS
    'Base32 secret para pyotp. NULL = 2FA no activado.';
COMMENT ON COLUMN seguridad.usuario.totp_confirmado_en IS
    'Timestamp cuando el usuario verificó el primer código TOTP. '
    'Hasta ese momento el secret está en "setup-mode" y el login normal sigue funcionando.';
COMMENT ON COLUMN seguridad.usuario.password_debe_cambiar IS
    'Si TRUE, el próximo login redirige a /password/cambiar antes de dejar entrar. '
    'Se pone TRUE al crear el usuario desde /admin/usuarios y cuando la policy '
    'detecta un password demasiado viejo.';
