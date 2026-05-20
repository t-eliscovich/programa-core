-- 0037_activos_orden_manual.sql
-- TMT 2026-05-20: pedido dueña — "Dejame drag and drop en activos.
-- porque asi lo ordeno manualmente".
--
-- Agrega columna `orden_manual` a scintela.activos para guardar el
-- orden personalizado. Filas con orden_manual NOT NULL se muestran
-- primero (en ese orden); las que no, caen al final con el orden
-- canónico por categoría / fecha.
--
-- Idempotente: el ALTER TABLE chequea IF NOT EXISTS via DO block.

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM information_schema.columns
     WHERE table_schema = 'scintela'
       AND table_name   = 'activos'
       AND column_name  = 'orden_manual'
  ) THEN
    EXECUTE 'ALTER TABLE scintela.activos ADD COLUMN orden_manual INTEGER';
  END IF;
END$$;

-- Index para que el ORDER BY orden_manual NULLS LAST sea rápido.
CREATE INDEX IF NOT EXISTS idx_activos_orden_manual
    ON scintela.activos (orden_manual NULLS LAST);

COMMIT;
