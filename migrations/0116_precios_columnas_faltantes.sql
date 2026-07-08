-- 0116_precios_columnas_faltantes.sql
-- TMT 2026-07-07: la tabla scintela.precios YA existía en prod (seed viejo),
-- así que el CREATE de 0114 (guardado con IF to_regclass IS NULL) se saltó y
-- la tabla puede NO tener las columnas `actualizado` / `usuario_edita` que usa
-- el UPDATE inline de la Lista de precios → el editar tiraba error.
-- Esta migración las agrega si faltan. Idempotente y re-corrible.
DO $$
BEGIN
    IF to_regclass('scintela.precios') IS NOT NULL THEN
        ALTER TABLE scintela.precios
            ADD COLUMN IF NOT EXISTS actualizado   TIMESTAMP   DEFAULT CURRENT_TIMESTAMP;
        ALTER TABLE scintela.precios
            ADD COLUMN IF NOT EXISTS usuario_edita VARCHAR(50);
    END IF;
END $$;
