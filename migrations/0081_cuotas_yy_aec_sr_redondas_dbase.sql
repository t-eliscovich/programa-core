-- Migración 0081: corrige las cuotas diarias A,E,C y SR a los valores
-- reales del dBase de HOY (revierte la 0080).
--
-- Contexto (TMT 2026-06-08):
--   La dueña pidió "usar lo mismo que dBase" para la acumulación diaria de
--   las provisiones YY. Revisando POSDAT.DBF + MENU.PRG (líneas 282-333)
--   frescos de hoy, el dBase suma estos montos fijos por día (no /25):
--       RT (IVA)          8400   ✓ ya OK (hardcoded en queries.buscar)
--       A,E,C AG,EN,CMB   7700   ← PC tenía 7300 (mig 0080)  ❌
--       SUELDOS           6000   ✓
--       SR / SRI Imp.Renta 3300  ← PC tenía 2700 (mig 0080)  ❌
--       SS IESS           2400   ✓
--       AB PROVISION      1300   ✓
--       13 DEC.TERCERO    1000   ✓
--       ALQUILER           700   ✓
--       PROV.INCOBRABLE    400   ✓
--       14 DEC.CUAR+RES    300   ✓
--       INTERESES          300   ✓
--       JP JUB.PATRONAL    200   ✓
--
--   La migración 0080 (2026-06-03) había bajado A,E,C→7300 y SR→2700
--   citando el legacy, pero el MENU.PRG actual dice 7700 y 3300 — y el
--   propio scripts/check_salud_dia.py espera SR=3300/día. Esta migración
--   los devuelve a los valores canónicos del dBase.
--
--   Sólo corrige la TASA diaria (scintela.provisiones). El nivel
--   persistido (scintela.posdat.importe) no se toca acá — eso lo realinea
--   el reconcile /admin/posdat-reconcile cuando la dueña lo aplique.

BEGIN;

UPDATE scintela.provisiones SET importe = 7700
 WHERE UPPER(TRIM(concepto)) LIKE 'A,E,C%';

UPDATE scintela.provisiones SET importe = 3300
 WHERE UPPER(TRIM(concepto)) LIKE 'SR%';

DO $$
DECLARE aec NUMERIC; sr NUMERIC;
BEGIN
    SELECT importe INTO aec FROM scintela.provisiones
     WHERE UPPER(TRIM(concepto)) LIKE 'A,E,C%' LIMIT 1;
    SELECT importe INTO sr FROM scintela.provisiones
     WHERE UPPER(TRIM(concepto)) LIKE 'SR%' LIMIT 1;
    RAISE NOTICE 'Mig 0081: A,E,C=% (esperado 7700), SR=% (esperado 3300)', aec, sr;
END $$;

COMMIT;
