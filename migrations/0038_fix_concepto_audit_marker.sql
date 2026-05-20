-- 0038_fix_concepto_audit_marker.sql
-- TMT 2026-05-20: antes del bug fix en posdat.queries.editar(), cuando
-- se editaba SÓLO el importe inline, el handler reemplazaba el concepto
-- original con el marker de audit "[ED imp_prev:X nuevo:Y]". El bug
-- está arreglado en el código, PERO los rows que ya quedaron corruptos
-- en la DB no tienen forma de recuperar el concepto original.
--
-- Esta migración detecta esas filas y limpia el concepto a NULL para
-- que sean fáciles de identificar visualmente en /posdat — la dueña
-- después los edita uno por uno con el botón "Editar" y completa el
-- concepto correcto.
--
-- Pattern: el concepto entero matchea "[ED imp_prev:NN.NN nuevo:NN.NN]"
-- (sin nada más). Si hay texto extra (caso raro: concepto válido +
-- marker pegado), NO lo tocamos.

BEGIN;

UPDATE scintela.posdat
   SET concepto = NULL
 WHERE concepto ~ '^\[ED imp_prev:[0-9.]+ nuevo:[0-9.]+\]$';

-- Esperado: ~1-5 filas (depende de cuántas veces se editó importe
-- inline antes del fix). Para el caso conocido: row #151.

COMMIT;
