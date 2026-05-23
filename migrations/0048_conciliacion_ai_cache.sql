-- 0048_conciliacion_ai_cache.sql
-- TMT 2026-05-23 — Cache de categorización por IA (Claude Haiku).
--
-- Para no llamar a la API cada vez que aparece el mismo concepto, cacheamos
-- por (concepto_normalizado, tipo). El concepto se normaliza sacando números
-- variables (fechas, referencias de tx específicas) para que "INTELA C-PAG-DHL
-- 05 15" y "INTELA C-PAG-DHL 06 02" compartan cache.

BEGIN;

CREATE TABLE IF NOT EXISTS scintela.conciliacion_ai_cache (
    id              BIGSERIAL    PRIMARY KEY,
    concepto_norm   TEXT         NOT NULL,
    tipo            TEXT         NOT NULL CHECK (tipo IN ('C','D','?')),
    categoria       TEXT         NOT NULL,
    grupo           TEXT         NOT NULL,
    label           TEXT         NOT NULL,
    cliente         TEXT,
    descripcion     TEXT,
    confianza       NUMERIC(3,2) NOT NULL DEFAULT 0.5,
    modelo          TEXT         NOT NULL DEFAULT 'claude-haiku-4-5',
    creado_en       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    hits            INTEGER      NOT NULL DEFAULT 1,
    ultimo_hit      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_aicache_concepto_tipo
    ON scintela.conciliacion_ai_cache (concepto_norm, tipo);

CREATE INDEX IF NOT EXISTS ix_aicache_ultimo_hit
    ON scintela.conciliacion_ai_cache (ultimo_hit DESC);

COMMIT;
