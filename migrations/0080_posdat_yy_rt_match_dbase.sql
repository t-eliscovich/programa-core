-- Migración 0080: PC posdat YY/RT == dBase (sin cierre mensual + backfill).
--
-- Contexto (TMT 2026-06-03):
--   La dueña pidió: "quiero que el programa quede igual que dBase para
--   posdatados". Las cifras observadas:
--      PC YY total = $186.277   ($70.800 YY + $115.477 RT)
--      dBase YY total = $720.624 ($595.400 YY + $125.224 RT)
--   Gap $534.347 = efecto del cierre mensual lazy de PC (reseteaba
--   importe=0 + baseline=fin de mes) contra el dBase que NUNCA cerró
--   y acumula desde 2009-2014.
--
-- Decisiones aplicadas:
--   (1) REMOVED cierre mensual lazy en modules/posdat/queries.py — el
--       display-time ahora acumula perpetuo, alineado con dBase.
--   (2) Revierte 2 cuotas drift que PC tenía distintas a dBase:
--          A,E,C  PC$7.700 → dBase $7.300  (Δ -400)
--          SRI    PC$3.300 → dBase $2.700  (Δ -600)
--       Fuente canónica: MENU.PRG L283-333 del legacy dBase.
--   (3) Backfill de importes YY/RT a los valores dBase actuales, con
--       baseline_date = hoy, así PC arranca matcheando y acumula a
--       partir de hoy igual que dBase desde mañana.
--
-- Los mov_doble tipo='posdat_yy_cierre_mes' previos NO se borran — son
-- audit histórico inmutable del experimento mayo-junio 2026.

BEGIN;

-- ───────────────────── 1. Revertir cuotas drift ──────────────────────
UPDATE scintela.provisiones SET importe = 7300
 WHERE UPPER(TRIM(concepto)) LIKE 'A,E,C%';

UPDATE scintela.provisiones SET importe = 2700
 WHERE UPPER(TRIM(concepto)) LIKE 'SR%';

-- ──────────── 2. Backfill importes YY/RT a snapshot dBase ────────────
-- Fuente: POSDAT.DBF (sync 2026-06-03). Tag [BACKFILL_DBASE_YY] en
-- usuario_modifica para trazabilidad.
--
-- Valores TARGET (importe que debe quedar persistido hoy 2026-06-03):
--   PROV.INCOBRABLE   $152.800
--   AB PROVISION      $119.400
--   SRI PROVISION     $108.900
--   13 DEC.TERCERO     $69.100
--   JP JUB.PATRONAL    $34.000
--   A,E,C AG,EN,CMB    $33.900
--   14 DEC.CUAR+RES    $33.500
--   SS IESS            $16.900
--   ALQUILER           $11.300
--   INTERESES           $9.500
--   SUELDOS             $6.100
--   RT                $125.224
--
-- Con baseline_date = hoy, _aplicar_display_time_yy() devolverá:
--   importe_display = importe_persistido + cuota_diaria × 0 = importe_persistido
-- → coincide EXACTO con dBase al momento del backfill.
-- Desde mañana ambos sistemas suman su cuota_diaria diaria.

UPDATE scintela.posdat SET
    importe = 152800,
    baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy',
    fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'PROV.INCOB%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 119400, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'AB %'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 108900, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'SRI%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 69100, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE '13%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 34000, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'JP%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 33900, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'A,E,C%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 33500, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE '14%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 16900, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) LIKE 'SS%'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 11300, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) = 'ALQUILER'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 9500, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) = 'INTERESES'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

UPDATE scintela.posdat SET
    importe = 6100, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'YY'
   AND UPPER(TRIM(concepto)) = 'SUELDOS'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

-- RT (IVA): única fila con prov='RT'. baseline=hoy para que la lógica
-- nueva (que ahora INCLUYE RT) arranque limpia.
UPDATE scintela.posdat SET
    importe = 125224, baseline_date = CURRENT_DATE,
    usuario_modifica = 'backfill_dbase_yy', fecha_modifica = CURRENT_TIMESTAMP
 WHERE UPPER(TRIM(prov)) = 'RT'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);

-- ───────────────────────── 3. Verificación ──────────────────────────
-- Total esperado ≈ $720.624:
--   YY 11 filas = 595.400 + RT 1 fila = 125.224 = 720.624.
-- El SELECT abajo solo es para que el output de migrate.py muestre
-- el total post-update. NOTICE en lugar de RAISE para no abortar.
DO $$
DECLARE total_yy_rt NUMERIC;
BEGIN
    SELECT COALESCE(SUM(importe), 0) INTO total_yy_rt
      FROM scintela.posdat
     WHERE UPPER(TRIM(prov)) IN ('YY', 'RT')
       AND COALESCE(banc, 0) = 0
       AND (anulada IS NOT TRUE OR anulada IS NULL);
    RAISE NOTICE 'Mig 0080: total YY/RT post-backfill = % (esperado ~720624)', total_yy_rt;
END $$;

COMMIT;
