-- 0031_mov_doble_batch.sql — batch_id para agrupar movs de una operación
-- TMT 2026-05-15
--
-- Contexto: una operación de UI puede generar varios mov_doble (multi-cheque
-- aplicado a varias facturas, transferencia que toca 2 bancos, endoso que
-- baja cheque y abona compra, etc.). Hoy aparecen como filas separadas y
-- el reverso opera fila por fila — si la dueña revierte sólo una, se queda
-- un estado inconsistente.
--
-- Solución: cada operación que crea >1 mov_doble genera un UUID `batch_id`
-- al inicio y todas las filas del mismo submit lo comparten. El /historial
-- agrupa por batch_id y el reverso atómico revierte TODAS las filas del
-- batch dentro de una sola transacción.
--
-- Compat 100% con legacy:
--   batch_id IS NULL  → mov suelto (todos los 6.6k movs viejos)
--   batch_id NOT NULL → integrante de una operación batch
--
-- Idempotente: ADD COLUMN IF NOT EXISTS, índice parcial con IF NOT EXISTS.

-- 1. Columna batch_id. NULL por default = compat 100% con legacy.
ALTER TABLE scintela.mov_doble
    ADD COLUMN IF NOT EXISTS batch_id UUID NULL;

COMMENT ON COLUMN scintela.mov_doble.batch_id IS
    'UUID que agrupa movs de la misma operación de UI (ej: 1 cheque '
    'aplicado a 3 facturas → 3 filas con el mismo batch_id). NULL = '
    'mov suelto (legacy). El reverso atómico de /historial revierte '
    'todas las filas del batch en una sola transacción.';

-- 2. Índice parcial sobre batch_id (sólo filas que tienen valor).
--    Acelera la query "dame todas las del batch X" del reverso atómico
--    sin pagar overhead de mantenimiento sobre los 6.6k legacy.
CREATE INDEX IF NOT EXISTS idx_mov_doble_batch_id
    ON scintela.mov_doble (batch_id)
    WHERE batch_id IS NOT NULL;

COMMENT ON INDEX scintela.idx_mov_doble_batch_id IS
    'Índice parcial para lookup rápido de hermanos de batch en el reverso '
    'atómico. Sólo indexa filas con batch_id NOT NULL (default es NULL = '
    'mov legacy suelto).';
