-- 0095_backfill_doble_cliente_cmj_176612.sql
-- TMT 2026-06-11 — cierre Δ TOTF (+4.896,63): doble por cliente distinto.
--
-- Factura SRI 176612 (02/06/2026, $198,18) está UNA sola vez en el dBase
-- (FACTURAS.DBF, cliente CJM, viva Z saldo 198,18). PC la tiene DOS veces:
--   · id 177899  cliente CMJ  [asinfo-carga, 09/06 22:38] ← NO aparea
--   · (dbf-import, cliente CJM) espejo exacto del dBase   ← aparea 1 a 1
-- CMJ (Jaramillo Maza) y CJM (Cajas Mora) son clientes REALES distintos,
-- NO es un typo de alias → no se agrega cliente_alias. Asinfo factura a
-- CMJ; el dBase la tipeó bajo CJM. dBase gana (criterio dueña): se
-- desactiva SOLO la fila que no aparea (carga → backfill, deja de contar,
-- no se borra nada). Si la fábrica corrige el cliente en el dBase, el
-- próximo sync lo absorbe solo.
--
-- IDEMPOTENTE: el WHERE pide usuario_crea='asinfo-carga'; re-correrla
-- no toca nada.
UPDATE scintela.factura
   SET usuario_crea = 'asinfo-backfill'
 WHERE id_factura = 177899
   AND usuario_crea = 'asinfo-carga'
   AND TRIM(codigo_cli) = 'CMJ'
   AND numf_completo LIKE '%176612';
