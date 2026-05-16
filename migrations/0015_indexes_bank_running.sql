-- =====================================================================
-- 0015_indexes_bank_running
-- =====================================================================
-- Índices que acelera el cálculo del saldo running en
-- `transacciones_bancarias` y `caja`. Necesarios para que
-- bank_helpers.insert_movimiento_bancario() y
-- caja_helpers.insert_movimiento_caja() corran en O(log n) por inserción
-- en vez de O(n).
--
-- Diseño de los índices:
--   transacciones_bancarias (no_banco, fecha DESC, id_transaccion DESC)
--     → soporta el _saldo_previo() lookup: "última fila del banco X
--       anterior a esta fecha".
--   transacciones_bancarias (no_banco, no_cta, fecha, id_transaccion)
--     → soporta walk-forward por banco+cuenta cuando hay multi-cuenta.
--   caja (fecha DESC, id_caja DESC)
--     → mismo principio para caja (mono-cuenta).
--
-- Idempotente: CREATE INDEX IF NOT EXISTS.
-- =====================================================================

-- transacciones_bancarias — _saldo_previo lookup
CREATE INDEX IF NOT EXISTS idx_txbanc_running_saldo
    ON scintela.transacciones_bancarias (no_banco, fecha DESC, id_transaccion DESC);

-- transacciones_bancarias — walk-forward por banco+cuenta
CREATE INDEX IF NOT EXISTS idx_txbanc_walk_forward
    ON scintela.transacciones_bancarias (no_banco, no_cta, fecha, id_transaccion);

-- caja — _saldo_previo lookup
CREATE INDEX IF NOT EXISTS idx_caja_running_saldo
    ON scintela.caja (fecha DESC NULLS LAST, id_caja DESC);

-- caja — walk-forward
CREATE INDEX IF NOT EXISTS idx_caja_walk_forward
    ON scintela.caja (fecha, id_caja);

COMMENT ON INDEX scintela.idx_txbanc_running_saldo IS
    'Acelera bank_helpers._saldo_previo() — lookup del saldo anterior a una '
    'fecha en un banco dado, ordenado por fecha DESC.';

COMMENT ON INDEX scintela.idx_caja_running_saldo IS
    'Acelera caja_helpers._saldo_previo() — saldo anterior a una fecha.';
