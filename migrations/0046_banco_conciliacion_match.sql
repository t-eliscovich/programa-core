-- 0046_banco_conciliacion_match.sql
-- TMT 2026-05-22 — Conciliación bidireccional banco real (xlsx) vs BANCSIS.
--
-- Cada fila representa un match (o aceptación unilateral) entre un movimiento
-- del extracto del banco y una transacción de BANCSIS. Los registros aquí:
--   1. NO vuelven a aparecer como "por conciliar" en sesiones futuras.
--   2. Se rendera el "check verde" en el listado BANCSIS.
--
-- Tipos de estado:
--   'matched'        — ambos lados coinciden: hay tx BANCSIS Y mov REAL.
--   'real_only_ok'   — solo está en REAL, vos aceptaste que es una diferencia
--                      legítima (ej: el banco te cargó algo que BANCSIS no
--                      registra). id_transaccion NULL.
--   'bancsis_only_ok'— solo está en BANCSIS, vos aceptaste que NO entró al
--                      banco aún o quedó sin sincronizar (poco común).
--                      datos real_* NULL.
--
-- Clave de dedupe del movimiento REAL: (real_fecha + real_documento + real_monto + real_tipo).
-- Si volves a subir el mismo xlsx, no se duplican filas.

BEGIN;

CREATE TABLE IF NOT EXISTS scintela.banco_conciliacion_match (
    id              BIGSERIAL   PRIMARY KEY,
    no_banco        INTEGER     NOT NULL,
    estado          TEXT        NOT NULL CHECK (estado IN ('matched','real_only_ok','bancsis_only_ok')),

    -- Lado REAL (extracto del banco)
    real_fecha      DATE,
    real_concepto   TEXT,
    real_documento  TEXT,                  -- ref/comprobante del banco
    real_monto      NUMERIC(14,2),
    real_tipo       TEXT,                  -- 'C' (crédito) o 'D' (débito)
    real_codigo     TEXT,                  -- código oficina (001045, etc.)
    real_oficina    TEXT,

    -- Lado BANCSIS (scintela.transacciones_bancarias.id_transaccion)
    id_transaccion  INTEGER,

    -- Auditoría
    usuario         TEXT        NOT NULL DEFAULT 'web',
    creado_en       TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Dedupe REAL: misma firma (fecha + documento + monto + tipo) no duplica matches.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bcm_real_firma
    ON scintela.banco_conciliacion_match
       (no_banco, real_fecha, real_documento, real_monto, real_tipo)
    WHERE real_documento IS NOT NULL;

-- Cada tx BANCSIS puede estar conciliada una sola vez.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bcm_bancsis
    ON scintela.banco_conciliacion_match (id_transaccion)
    WHERE id_transaccion IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bcm_banco_fecha
    ON scintela.banco_conciliacion_match (no_banco, real_fecha DESC);

COMMIT;
