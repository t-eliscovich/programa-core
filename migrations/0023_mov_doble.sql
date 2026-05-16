-- 0023_mov_doble.sql — Historial de movimientos dobles (TMT 2026-05-12)
--
-- Toda operación cruzada (caja↔banco, cheque endosado a compra, capital↔banco,
-- transferencia banco↔banco, etc.) deja una fila en scintela.mov_doble.
-- Los reversos también se registran y enlazan a su original via id_original.
--
-- Diseño:
--   - estado='activo'    → la operación está vigente.
--   - estado='reversado' → existe un reverso que la anuló (campo id_reverso apunta).
--   - estado='reverso'   → esta fila ES un reverso de otra (campo id_original apunta).
--
-- Filosofía: nunca borrar filas (audit trail completo). Para "anular" se
-- crea otra fila de tipo reverso y se marca la original como reversada.

CREATE TABLE IF NOT EXISTS scintela.mov_doble (
    id_mov_doble    BIGSERIAL PRIMARY KEY,
    fecha_creacion  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fecha_operacion DATE NOT NULL,
    tipo            TEXT NOT NULL,
    origen_table    TEXT NOT NULL,
    origen_id       BIGINT NOT NULL,
    destino_table   TEXT NOT NULL,
    destino_id      BIGINT NOT NULL,
    importe         NUMERIC(18,2) NOT NULL,
    concepto        TEXT,
    usuario         TEXT,
    estado          TEXT NOT NULL DEFAULT 'activo'
                    CHECK (estado IN ('activo', 'reversado', 'reverso')),
    id_reverso      BIGINT REFERENCES scintela.mov_doble(id_mov_doble),
    id_original     BIGINT REFERENCES scintela.mov_doble(id_mov_doble),
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_mov_doble_fecha
    ON scintela.mov_doble(fecha_operacion DESC, id_mov_doble DESC);

CREATE INDEX IF NOT EXISTS idx_mov_doble_origen
    ON scintela.mov_doble(origen_table, origen_id);

CREATE INDEX IF NOT EXISTS idx_mov_doble_destino
    ON scintela.mov_doble(destino_table, destino_id);

CREATE INDEX IF NOT EXISTS idx_mov_doble_tipo
    ON scintela.mov_doble(tipo);

CREATE INDEX IF NOT EXISTS idx_mov_doble_estado
    ON scintela.mov_doble(estado);

COMMENT ON TABLE scintela.mov_doble IS
    'Historial unificado de movimientos cruzados (caja↔banco, compra↔banco, etc). '
    'Reversos crean una nueva fila con id_original = la original y marcan a la '
    'original con estado=reversado + id_reverso = la nueva.';
