-- 0086_yy_rt_baseline_backfill.sql
-- TMT 2026-06-10 — deep-dive YY: toda fila YY/RT viva debe tener
-- baseline_date para que exista UN SOLO motor de acumulación
-- (persistir_acumulacion_yy). Las filas con baseline NULL caían en el
-- motor legacy de correr_provisiones_diarias (cron por sistema_meta) —
-- dos motores escribiendo el mismo campo = drift y dobles conteos.
-- El reconcile viejo insertaba YY sin baseline (ya corregido en código);
-- acá saneamos las que hayan quedado.
UPDATE scintela.posdat
   SET baseline_date = CURRENT_DATE
 WHERE UPPER(TRIM(COALESCE(prov, ''))) IN ('YY', 'RT')
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND baseline_date IS NULL;
