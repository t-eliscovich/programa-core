-- 0190_histos_codigo.sql
-- ⚠ RENUMERADA el 2026-08-13. Nació como `0064_histos_codigo` y NUNCA CORRIÓ:
-- producción ya tenía el número 0064 tomado por
-- `0064_reset_importes_yy_snapshot_original`. Sin esta columna, "Restaurar
-- borradas" de la papelera insertaba `codigo` en una tabla que no lo tiene y
-- fallaba. El contenido es el original, sin tocar.

-- Agregar columna `codigo` a banco_historicos_pendientes — TMT 2026-06-02.
--
-- Dueña: 'y codigo importa tambien'. El codigo del extracto Pichincha
-- distingue tipos de cargo cuando comparten documento (ej. cheque
-- devuelto trae 3 filas con mismo doc pero codigos distintos: 001314
-- = cheque, 098450 = IVA, 098426 = costo). Sin codigo, el dedupe podría
-- colapsar mal filas legítimas relacionadas.
--
-- Las filas legacy backfilleadas (migs 0056-0058) quedan con codigo=NULL
-- porque el load original no preservó esa columna. Para esas filas, el
-- dedupe usa la firma sin codigo (4 campos). Para las nuevas (uploads
-- post-mig-0064), codigo se popula automáticamente y participa en la
-- firma de 5 campos.

ALTER TABLE scintela.banco_historicos_pendientes
    ADD COLUMN IF NOT EXISTS codigo VARCHAR(20);

CREATE INDEX IF NOT EXISTS idx_bhp_doc_codigo
    ON scintela.banco_historicos_pendientes (no_banco, documento, codigo)
 WHERE conciliado_en IS NULL;
