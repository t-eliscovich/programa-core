-- 0107_importacion_recepcion_deuda.sql
-- TMT 2026-06-29 (dueña): nuevo flujo e2e de importaciones.
--
-- Cuando LLEGA el hilo (todavía sin pagar) se le asigna un COSTO ESTIMADO
-- (promedio histórico US$/kg del proveedor, editable) y se genera una DEUDA
-- = costo_estimado - anticipo aplicado. Los kilos entran al stock al recibir.
-- Cuando se PAGA de verdad (ej. estimado 30k, real 32k) se sobrescribe la
-- deuda con el monto real (sin asiento de ajuste) y se marca pagada.
--
-- Reemplaza el viejo par 'contabilizar' (todo-o-nada) + 'monto_pagado'
-- (informativo) de la migración 0104. Esas columnas quedan pero ya no se usan.
--
-- PC-only: extiende scintela.importacion_pago, que NO está en el TABLE_MAP del
-- Sync dBase, así que sobrevive el TRUNCATE+INSERT de scintela.compra.
-- Idempotente (ADD COLUMN IF NOT EXISTS).
DO $$
BEGIN
  IF to_regclass('scintela.importacion_pago') IS NULL THEN
    RAISE EXCEPTION 'Falta la migración 0104 (scintela.importacion_pago).';
  END IF;
END $$;

ALTER TABLE scintela.importacion_pago
  ADD COLUMN IF NOT EXISTS recibido_pc        BOOLEAN       NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS fecha_recepcion_pc DATE,
  ADD COLUMN IF NOT EXISTS kg_recibidos       NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS costo_estimado     NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS anticipo_aplicado  NUMERIC(14,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS deuda              NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS pagada             BOOLEAN       NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS fecha_pago         DATE,
  ADD COLUMN IF NOT EXISTS monto_real         NUMERIC(14,2);
