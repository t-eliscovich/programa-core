-- Migration 0062: corregir baseline_date YY a zona Ecuador.
--
-- Contexto (TMT 2026-05-28 noche): la migración 0061 usó CURRENT_DATE
-- que corre en zona del server (UTC). Eso dejó baseline_date='2026-05-29'
-- aunque en Ecuador todavía era 28/05 cuando la dueña aplicó la migración.
-- Resultado: mañana viernes 29/05 EC, el offset display-time = 0 y los
-- importes NO suben.
--
-- Fix: forzar baseline_date a 2026-05-28 (HOY zona Ecuador) para todas
-- las YY abiertas que tengan baseline 29/05. Mañana viernes en EC el
-- offset = 1 y cada YY sube cuota_diaria. Como pidió la dueña.
--
-- Idempotente: el WHERE filtra solo a las YY abiertas con baseline 29/05.
-- Si la migración se aplica por error 2 veces, la segunda no toca nada.

UPDATE scintela.posdat
   SET baseline_date = DATE '2026-05-28'
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND baseline_date = DATE '2026-05-29';

-- También dejamos el marker del cron viejo en zona EC para consistencia.
UPDATE scintela.sistema_meta
   SET valor = '2026-05-28', actualizado = CURRENT_TIMESTAMP
 WHERE clave = 'provisiones_diarias_ult_fecha'
   AND valor = '2026-05-29';
