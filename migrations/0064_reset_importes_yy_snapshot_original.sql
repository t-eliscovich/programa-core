-- Migration 0064: resetear los importes YY a los del snapshot original.
--
-- Contexto (TMT 2026-05-28 noche, sesión replanear):
--   Después de los pushes de hoy, el total YY estaba en 693.200 vs el
--   snapshot original 639.800 — diferencia de 53.400 EXACTOS por bumps
--   del cron viejo (sumó 1 día con cuotas viejas grandes: 12000+14000+
--   5000+14000+2100+500+1000+1000+3100+200+500 = 53.400).
--   La dueña pidió volver a los importes originales para empezar de
--   cero ahora que las cuotas correctas están cargadas (migración 0063).
--
-- Cada UPDATE matchea por (UPPER(concepto) LIKE '...') igual que en 0063
-- — mismo patrón. Idempotente: si los importes ya están en el target,
-- el UPDATE no hace daño.
--
-- También resetea baseline_date a 2026-05-28 (HOY en zona EC) por las
-- dudas, así offset arranca limpio.

UPDATE scintela.posdat
   SET importe       = 8300,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'INTER%';

UPDATE scintela.posdat
   SET importe       = 33100,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'A,E,C%';

UPDATE scintela.posdat
   SET importe       = 86100,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'SUELDOS%';

UPDATE scintela.posdat
   SET importe       = 8500,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) = 'ALQUILER';

UPDATE scintela.posdat
   SET importe       = 7300,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'SS%';

UPDATE scintela.posdat
   SET importe       = 32300,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE '14%';

UPDATE scintela.posdat
   SET importe       = 65100,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE '13%';

UPDATE scintela.posdat
   SET importe       = 114200,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'AB%';

UPDATE scintela.posdat
   SET importe       = 95700,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'SR%';

UPDATE scintela.posdat
   SET importe       = 33200,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'JP%';

UPDATE scintela.posdat
   SET importe       = 156000,
       baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND UPPER(TRIM(COALESCE(concepto, ''))) LIKE 'PROV.INCOB%';
