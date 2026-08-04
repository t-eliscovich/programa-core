-- 0158 — scintela.cheque.concepto
--
-- TMT 2026-08-04. Alex, por WhatsApp, sobre el resumen de cobranza del día:
--   "podemos colocar de forma manual un concepto cuando sea anticipo … o
--    tener preestablecido: Abono a cheques, anticipo a facturas … es más
--    porque esto se entrega a contabilidad y no van a saber qué hacer con el
--    'sin aplicar facturas'"
--
-- La tabla NO tenía columna `concepto` (el campo del form de edición se
-- apendeaba a `observacion` con tag `[C]`, que es una bitácora append-only y
-- no se puede leer como campo). Va columna propia, corta, opcional.
--
-- ⚠ El deploy NO corre migraciones: la columna también se bootstrapea en
-- caliente desde `modules/cheques/concepto_cobro.py::bootstrap_columna`.
-- Este archivo existe para que el esquema quede documentado y para entornos
-- nuevos.

ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS concepto VARCHAR(60);

COMMENT ON COLUMN scintela.cheque.concepto IS
    'Concepto libre del cobro (max 60). Se imprime en el resumen de cobranza '
    'del día cuando el cobro no se aplica a facturas. Complementa al rótulo '
    'automático que se deriva de mov_doble.';
