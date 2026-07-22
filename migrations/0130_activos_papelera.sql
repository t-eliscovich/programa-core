-- 0130 — Papelera / soft-delete de activos fijos (retención 30 días).
-- TMT 2026-07-22 (dueña): poder BORRAR activos duplicados por la UI de
-- /activos, reversible. Ej: "+TERRENO SUR" y "TERRENO 6550m2" son el
-- detalle de "TOTAL TERR.CALD" (510k+145k=655k) → contarlos duplica.
--
-- No hay DELETE duro: `borrado_en` marca la fila. Queda excluida de las
-- listas y del balance (activos_totales) pero restaurable desde
-- /activos/papelera dentro de la ventana de retención. `borrado_por`
-- registra el usuario. Idempotente (IF NOT EXISTS): re-correr es seguro.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'scintela'
       AND table_name   = 'activos'
       AND column_name  = 'borrado_en'
  ) THEN
    EXECUTE 'ALTER TABLE scintela.activos ADD COLUMN borrado_en TIMESTAMPTZ';
    EXECUTE 'ALTER TABLE scintela.activos ADD COLUMN borrado_por TEXT';
  END IF;
END $$;

-- Index parcial: acelera el filtro "borrado_en IS NULL" (el caso normal).
CREATE INDEX IF NOT EXISTS ix_activos_vivos
    ON scintela.activos (id_activos) WHERE borrado_en IS NULL;
