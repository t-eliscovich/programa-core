-- 0129 — Papelera de movimientos de banco borrados (retención 30 días).
-- TMT 2026-07-22 (dueña): "chequear que la data se esté guardando por 30 días,
-- por si hay que restaurar". Hoy el borrado de un movimiento cargado en PC es un
-- DELETE duro (bancos.eliminar_movimiento_pc): no queda snapshot ni forma de
-- restaurar desde la app. Esta tabla guarda la fila completa (to_jsonb) al
-- borrar; la vista /bancos/papelera la lista y permite RESTAURAR dentro de los
-- 30 días. Después de 30 días se purga (retención).
--
-- Idempotente (IF NOT EXISTS): re-correr es seguro.

CREATE TABLE IF NOT EXISTS scintela.papelera_movimiento_banco (
    id_papelera     BIGSERIAL PRIMARY KEY,
    id_transaccion  INTEGER      NOT NULL,   -- id original de la fila borrada
    no_banco        INTEGER,
    banco_nombre    TEXT,
    documento       TEXT,
    importe         NUMERIC,
    fecha           DATE,
    concepto        TEXT,
    prov            TEXT,
    snapshot        JSONB        NOT NULL,    -- fila completa (to_jsonb) para restaurar
    borrado_por     TEXT,
    borrado_en      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    restaurado_en   TIMESTAMPTZ,             -- NULL = sigue restaurable
    restaurado_por  TEXT
);

CREATE INDEX IF NOT EXISTS ix_papelera_mov_banco_borrado_en
    ON scintela.papelera_movimiento_banco (borrado_en DESC);

CREATE INDEX IF NOT EXISTS ix_papelera_mov_banco_tx
    ON scintela.papelera_movimiento_banco (id_transaccion);
