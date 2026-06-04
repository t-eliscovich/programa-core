-- 0074: numreferencia_manual sobrevive al sync dBase.
--
-- La dueña edita inline en /bancos/N el N° de doc para conciliar por num.
-- Si guardamos en transacciones_bancarias.numreferencia, el próximo sync
-- DBF lo pisa con el valor del campo NUM del DBF (vacío o viejo).
--
-- Fix: nueva columna numreferencia_manual que el sync NO toca. El matcher
-- y la UI usan COALESCE(numreferencia_manual, numreferencia) — la edición
-- web gana sobre el DBF, salvo que se borre explícitamente.
--
-- TMT 2026-06-03
SET search_path = scintela, public;

ALTER TABLE scintela.transacciones_bancarias
    ADD COLUMN IF NOT EXISTS numreferencia_manual TEXT;

COMMENT ON COLUMN scintela.transacciones_bancarias.numreferencia_manual IS
'Edición manual via UI. NO se sobreescribe en sync dBase. El matcher usa COALESCE(numreferencia_manual, numreferencia).';

-- No hacemos backfill automático: no sabemos qué numreferencia vino de
-- dBase y cuál fue editado web. La dueña re-edita los pocos casos que ya
-- había marcado antes de esta mig (1 click) y a partir de ahora todos los
-- edits van a numreferencia_manual.
