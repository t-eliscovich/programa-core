-- 0039_conciliacion_manual_log.sql
-- TMT 2026-05-20 — log de auditoría de la conciliación bancaria manual.
--
-- Cada fila representa una decisión humana sobre un depósito del Excel:
--   - 'confirmado' → el match automático estaba bien
--   - 'rechazado'  → el match es incorrecto / no entró al banco
--   - 'pendiente'  → marcado para revisar después (sin decisión final)
--
-- La clave de un depósito del Excel es (fecha + valor + codigo + concepto)
-- normalizada. Como un mismo depósito puede aparecer varias veces si lo
-- subís de nuevo, usamos `firma_dep` (hash del depósito) como dedupe natural.
--
-- Idempotente: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
BEGIN;

CREATE TABLE IF NOT EXISTS scintela.conciliacion_manual_log (
    id              BIGSERIAL PRIMARY KEY,
    firma_dep       TEXT        NOT NULL,
    fecha_dep       DATE,
    valor_dep       NUMERIC(14,2) NOT NULL,
    codigo_dep      TEXT        NOT NULL DEFAULT '',
    concepto_dep    TEXT        NOT NULL DEFAULT '',
    accion          TEXT        NOT NULL CHECK (accion IN ('confirmado','rechazado','pendiente')),
    id_transaccion  INTEGER,          -- match elegido (FK a transacciones_bancarias)
    nota            TEXT,             -- opcional, motivo del rechazo
    usuario         TEXT        NOT NULL DEFAULT 'web',
    creado_en       TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_concil_log_firma
    ON scintela.conciliacion_manual_log (firma_dep);

CREATE INDEX IF NOT EXISTS ix_concil_log_fecha
    ON scintela.conciliacion_manual_log (creado_en DESC);

COMMIT;
