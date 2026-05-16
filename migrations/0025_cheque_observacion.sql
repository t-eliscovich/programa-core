-- 0025_cheque_observacion.sql
-- TMT 2026-05-13.
--
-- Bug encontrado en producción: `cheques.queries.endosar()` (línea 1096)
-- y `cheques.queries.marcar_error_carga()` (línea 470) hacen UPDATE
-- sobre `scintela.cheque.observacion`, pero la columna nunca existió.
-- El error explota como:
--     psycopg2.errors.UndefinedColumn: column "observacion" does not exist
--
-- La intención del autor era trazar marcas tipo
-- "[ENDOSO a ACR 2026-05-13 → compra #10000]" para auditoría — agregamos
-- la columna ahora, idempotente.

ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS observacion VARCHAR(500);

COMMENT ON COLUMN scintela.cheque.observacion IS
    'Bitácora append-only del cheque: rebotes ([X]…), endosos ([ENDOSO…]),'
    ' postergaciones, etc. Truncada a derecha (RIGHT()) para no crecer sin'
    ' fin. Distinta de cliente.observacion (notas del cliente).';
