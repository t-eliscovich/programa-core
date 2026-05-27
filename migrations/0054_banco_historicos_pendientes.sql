-- 0054_banco_historicos_pendientes.sql
-- TMT 2026-05-27 dueña: "necesito cargar unos movimientos que son los
-- historicos no conciliados de parte del banco, y deberian aparecer
-- siempre que hago la conciliacion, salvo que sean conciliados."
--
-- Holds bank deposits/credits that are historically pending — never
-- conciliated with PC. They get injected into the matcher's movs_real
-- on every conciliation upload, AND shown in a dedicated panel on
-- /conciliacion/banco/resultado so the user always sees the backlog.
--
-- When a match is confirmed against one of these (via the matcher or
-- manual click-to-pair), conciliado_match_id gets set pointing to the
-- new fila in banco_conciliacion_match — so it stops appearing.

BEGIN;

CREATE TABLE IF NOT EXISTS scintela.banco_historicos_pendientes (
    id                    BIGSERIAL PRIMARY KEY,
    no_banco              INTEGER NOT NULL,
    fecha                 DATE,                       -- fecha del movimiento (banco)
    concepto              TEXT,                       -- detalle / descripción
    documento             TEXT,                       -- código / doc banco
    monto                 NUMERIC(14,2) NOT NULL,     -- VALOR (positivo, son depósitos)
    tipo                  TEXT NOT NULL DEFAULT 'C',  -- C=crédito (default — son depósitos pendientes)
    oficina               TEXT,                       -- DETALLE oficina/agencia
    detalle               TEXT,                       -- columna extra DETALLE
    fuente                TEXT,                       -- 'xlsx:NombreHoja:fila' o 'manual'
    -- Cuando se concilia, apunta al match creado. Si está NULL → sigue pendiente.
    conciliado_match_id   BIGINT REFERENCES scintela.banco_conciliacion_match(id) ON DELETE SET NULL,
    conciliado_en         TIMESTAMP,
    conciliado_por        TEXT,
    creado_en             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    creado_por            TEXT DEFAULT 'web'
);

-- Index principal: pendientes por banco
CREATE INDEX IF NOT EXISTS idx_bhp_pendientes_no_banco
    ON scintela.banco_historicos_pendientes (no_banco, fecha DESC)
    WHERE conciliado_match_id IS NULL;

-- Dedupe: misma firma (banco + fecha + documento + monto) no se importa 2 veces.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bhp_firma
    ON scintela.banco_historicos_pendientes
       (no_banco, fecha, COALESCE(documento, ''), monto, tipo);

COMMIT;
