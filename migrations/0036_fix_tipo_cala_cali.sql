-- 0036_fix_tipo_cala_cali.sql
-- TMT 2026-05-20: la dueña marcó que el terreno "CALA CALI" en
-- scintela.activos figuraba con tipo='I' (intangible) cuando en realidad
-- es un terreno físico → tipo='T'.
--
-- Reglas seguras: solo toca filas donde tipo='I' Y el concepto matchea
-- CALA CALI. Si más adelante aparece otro intangible con ese nombre,
-- esta migración NO lo va a tocar dos veces porque a esta altura ya va
-- a estar en T.

BEGIN;

UPDATE scintela.activos
   SET tipo = 'T'
 WHERE UPPER(COALESCE(tipo, '')) = 'I'
   AND UPPER(COALESCE(concepto, '')) LIKE '%CALA CALI%';

-- Sanity check: dejar log de cuántas filas cambiaron (psql lo muestra).
-- Esperado: 1 fila (el terreno Cala Cali).

COMMIT;
