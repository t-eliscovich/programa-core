-- 0096_backfill_7_asinfo_carga_mayo_sin_dbase.sql
-- TMT 2026-06-11 — cierre del Δ TOTF de las 7 facturas asinfo-carga de mayo
-- (+2.960,89). Decisión dueña: "si esto no lo tiene el dBase, no carguemos".
--
-- Búsqueda profunda en FACTURAS.DBF del tarball final (2026-06-11 10:21):
-- ninguna de las 7 aparece — ni por N° SRI (numf), ni por (cliente,importe),
-- ni por importe solo en ±60 días, ni por SALDO/ABONO, en NINGÚN stat
-- (incluidas T pagadas y X anuladas). Los clientes JDR/AEJ/JIA/VMD/AQN no
-- tienen NINGUNA factura en el DBF desde 01/04; EMD solo tiene 176500 (T,
-- $352,21) y 177145 (Z, $516,17), importes que NO matchean. Conclusión:
-- son facturas que Asinfo emitió y el dBase nunca tipeó.
--
--   N° SRI            cliente  importe   (id_factura PC card asinfo)
--   001-099-000175489  EMD      177,72    (id 178419)
--   001-099-000175658  JDR      174,89    (id 178392)
--   001-099-000175705  AEJ      322,20    (id 178385)
--   001-099-000175729  JIA      202,71    (id 178381)
--   001-099-000175965  VMD      774,59    (id 178352)
--   001-099-000176033  EMD      529,12    (id 178345)
--   001-099-000176400  AQN      779,66    (id 178120)
--
-- Acción: flip usuario_crea 'asinfo-carga' -> 'asinfo-backfill' (NO borra,
-- reversible, NO_BACKFILL_WHERE deja de contarlas en la utilidad/cartera
-- live). Si la fábrica las tipea en el dBase, el próximo sync las absorbe
-- solo (mismo numf_completo).
--
-- QUIRÚRGICO: key por numf_completo (sufijo SRI exacto) + usuario_crea +
-- cliente — igual patrón que mig 0095. NO toca dbf-import ni backfill.
-- IDEMPOTENTE: el WHERE pide usuario_crea='asinfo-carga'; re-correrla no-op.
-- GUARD to_regclass: no-op en test/CI donde scintela.factura no existe
-- (la CI se rompió una vez por omitirlo — ver mig 0094 b02ef1f).

DO $$
BEGIN
    IF to_regclass('scintela.factura') IS NULL THEN
        RAISE NOTICE 'scintela.factura no existe (test/CI) — skip 0096';
        RETURN;
    END IF;

    UPDATE scintela.factura
       SET usuario_crea = 'asinfo-backfill'
     WHERE usuario_crea = 'asinfo-carga'
       AND (
            (numf_completo LIKE '%175489' AND TRIM(codigo_cli) = 'EMD') OR
            (numf_completo LIKE '%175658' AND TRIM(codigo_cli) = 'JDR') OR
            (numf_completo LIKE '%175705' AND TRIM(codigo_cli) = 'AEJ') OR
            (numf_completo LIKE '%175729' AND TRIM(codigo_cli) = 'JIA') OR
            (numf_completo LIKE '%175965' AND TRIM(codigo_cli) = 'VMD') OR
            (numf_completo LIKE '%176033' AND TRIM(codigo_cli) = 'EMD') OR
            (numf_completo LIKE '%176400' AND TRIM(codigo_cli) = 'AQN')
       );

    RAISE NOTICE '0096: facturas asinfo-carga mayo flipeadas a backfill';
END $$;
