-- 0138_factura_observacion.sql
-- TMT 2026-07-29.
--
-- Exactamente el mismo bug que arregló 0025 para `cheque`, pero en
-- `factura` y todavía vivo: `/facturas/<id>/editar` tiene un campo
-- "observación" y `facturas.queries.editar()` (L256) hace
--
--     observacion = COALESCE(observacion||' | ','')||%s
--
-- sobre una columna que NO existe. Consecuencia real para el usuario:
-- editar el abono de una factura ANDA si dejás la observación vacía, y
-- FALLA con un error genérico si escribís algo — que es la peor
-- combinación posible, porque parece intermitente.
--
-- Salió a la luz el 29/07 armando /admin/salud/diag: la pantalla pidió
-- `f.observacion` para mostrar el contexto de las facturas descuadradas y
-- Postgres contestó `UndefinedColumn`.
--
-- Idempotente, igual que 0025.

ALTER TABLE scintela.factura
    ADD COLUMN IF NOT EXISTS observacion VARCHAR(200);

COMMENT ON COLUMN scintela.factura.observacion IS
    'Bitácora append-only de la factura: marcas [E] de edición de abono/condic,'
    ' motivos de anulación. Append con COALESCE(observacion||'' | '','''')||nuevo.'
    ' Agregada en 0138 — el código la escribía desde mayo sin que existiera.';
