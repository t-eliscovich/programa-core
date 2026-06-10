-- 0085_remark_backfill_facturas_2026_06_10.sql
-- TMT 2026-06-10 — las 3 facturas del 09/06 cargadas vía
-- /facturas/cargar-desde-asinfo-bulk ANTES de que se aplicara el trigger
-- 0084 quedaron con usuario_crea='tamara'. Las re-marcamos al marker
-- canónico para que NO_BACKFILL_WHERE las excluya de la utilidad live.
-- QUIRÚRGICO a propósito (lista explícita de numf_completo + guard de
-- usuario): un UPDATE genérico por regex podría recategorizar facturas
-- históricas del dBase y desplomar la cartera.
UPDATE scintela.factura
   SET usuario_crea = 'asinfo-backfill'
 WHERE numf_completo IN (
        '001-099-000177059',  -- NAI  $2.804,09
        '001-099-000177049',  -- FGJ  $730,94
        '001-099-000177035'   -- MNM  $1.198,38
       )
   AND COALESCE(usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import');
