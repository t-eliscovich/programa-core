-- 0053_conciliacion_upload.sql
-- TMT 2026-05-26 dueña: "necesito en conciliaciones ver los files ya subidos".
-- Tracking real de cada upload de extracto bancario. Cubre el caso de
-- "subí pero no confirmé matches" que la vista `ultimos_extractos()`
-- (basada en banco_conciliacion_match) no podía mostrar.

CREATE TABLE IF NOT EXISTS scintela.conciliacion_upload (
    id            BIGSERIAL PRIMARY KEY,
    no_banco      INTEGER NOT NULL,
    filename      TEXT,
    file_hash     TEXT,         -- sha256 de los primeros 64 hex chars
    n_filas       INTEGER,      -- movimientos parseados
    fecha_min     DATE,         -- min fecha en el extracto
    fecha_max     DATE,         -- max fecha en el extracto
    usuario       TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_concil_upload_banco_fecha
    ON scintela.conciliacion_upload (no_banco, creado_en DESC);
