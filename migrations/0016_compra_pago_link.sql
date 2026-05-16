-- =====================================================================
-- 0016_compra_pago_link
-- =====================================================================
-- Linkar una compra pagada al movimiento bancario o de caja que la pagó.
-- Permite reverse de compra pagada (encuentra la fila origen del movimiento)
-- y bloquea editar importe/fechad de compras ya pagadas.
--
-- Decisión 2026-04-30 (addendum batch 22 §D): cuando `crear()` recibe
-- pagada=True, además del INSERT en compra hace el INSERT bancario o de
-- caja. La columna nueva `id_transaccion` enlaza la compra con la fila
-- origen del egreso para auditoría y reverse.
--
-- `cuenta_pagada`: char(1) discriminador rápido. 'C'=caja, 'B'=banco
-- (no_banco está en la columna `no_banco` ya existente). NULL = no pagada.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS.
-- =====================================================================

ALTER TABLE scintela.compra
    ADD COLUMN IF NOT EXISTS id_transaccion bigint,
    ADD COLUMN IF NOT EXISTS cuenta_pagada  varchar(1);

CREATE INDEX IF NOT EXISTS idx_compra_id_transaccion
    ON scintela.compra (id_transaccion)
    WHERE id_transaccion IS NOT NULL;

COMMENT ON COLUMN scintela.compra.id_transaccion IS
    'FK lazy a transacciones_bancarias.id_transaccion (banco) o a '
    'caja.id_caja (cuando cuenta_pagada=C). NULL si la compra está '
    'pendiente de pago (posdat banc=0).';

COMMENT ON COLUMN scintela.compra.cuenta_pagada IS
    'Discriminador del pago: C=caja, B=banco (no_banco en columna existente). '
    'NULL = compra pendiente. Si llega una nueva compra y se paga al instante, '
    'se setea junto con id_transaccion.';
