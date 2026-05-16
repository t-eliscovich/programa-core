-- =====================================================================
-- 0004_bitacora
-- =====================================================================
-- Tabla de auditoría global (bitácora de acciones).
--
-- Cada escritura relevante (crear/editar/anular/aplicar/reversar) se
-- registra aquí con usuario, timestamp, módulo, entidad, id de la entidad,
-- IP y un blob JSON con los parámetros usados.
--
-- La lógica de inserción vive en Python (auth.registrar_bitacora) y se
-- dispara desde un after_request hook.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.bitacora_acciones (
    id_bitacora   BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usuario       VARCHAR(40)  NOT NULL,
    rol           VARCHAR(40),
    ip            VARCHAR(45),
    metodo        VARCHAR(8)   NOT NULL,  -- GET / POST / etc
    ruta          VARCHAR(200) NOT NULL,
    modulo        VARCHAR(40),
    accion        VARCHAR(40),
    entidad       VARCHAR(40),            -- factura, cheque, cliente…
    id_entidad    VARCHAR(60),            -- puede ser entero o código alfanumérico
    status_http   SMALLINT,
    payload       JSONB,
    resumen       VARCHAR(200)
);

CREATE INDEX IF NOT EXISTS idx_bitacora_ts        ON scintela.bitacora_acciones (ts DESC);
CREATE INDEX IF NOT EXISTS idx_bitacora_usuario   ON scintela.bitacora_acciones (usuario);
CREATE INDEX IF NOT EXISTS idx_bitacora_entidad   ON scintela.bitacora_acciones (entidad, id_entidad);
CREATE INDEX IF NOT EXISTS idx_bitacora_modulo    ON scintela.bitacora_acciones (modulo, accion);

COMMENT ON TABLE scintela.bitacora_acciones IS
    'Bitácora de acciones: auditoría global de escrituras (facturas, cheques, clientes, etc.).';
