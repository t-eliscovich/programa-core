-- 0131_byg_cheque_devuelto_opcion1.sql
-- TMT 2026-07-25 — Cheque rebotado de BYG: replicar el dBase (OPCIÓN 1, elegida
-- por la dueña: la deuda del rebote queda VIVA en el cheque devuelto).
--
-- SITUACIÓN (verificada contra el tarball del dBase 24/07 y la app live):
--   BYG entregó el cheque 14778 ($21.508,62, Pichincha), se depositó y REBOTÓ
--   (dBase CHEQUES.DBF STAT=V, FECHOUT 23/07). El banco de PC ya tiene la nota
--   de débito ("ch.devuelto 14778 BYG" −21.508,62) hecha y CONCILIADA (fix
--   23/07 "corregir devoluciones sin ND"). Lo que faltó: pasar el cheque a
--   DEVUELTO y bajar las facturas que ese cheque había pagado.
--   - En el dBase esas 5 facturas de abril están en T (cobradas por el cheque
--     que después rebotó): 172265, 172582, 172799, 174052, 174269 (Σ 21.635,09;
--     la 174269 lleva el pago grande abono 21.538,88).
--   - En PC quedaron VIVAS (el cheque nunca se aplicó a facturas acá,
--     chequesxfact=0) → inflan facturas +21.635.
--
-- QUÉ HACE (OPCIÓN 1 = paridad de comportamiento con el dBase pero SIN perder
-- la deuda: PC queda más correcto que el dBase, que directamente la suelta):
--   1. Las 5 facturas de abril → T (saldo 0), igual que el set-stat de la UI.
--      La 172799 ya se pasó a T a mano el 25/07 (el guard la saltea).
--   2. El cheque 14778 (depositado, stat B) → estado 1 = "Devuelto". Es el
--      relabel PLANO (mismo efecto que transicionar_stat B→1, el `else` sin
--      side-effects). Entra a cartera de cheques (TOTC cuenta 1) = la deuda
--      sigue viva, ahora en el cheque devuelto en vez de en las facturas.
--
-- NO TOCA EL BANCO → la conciliación queda INTACTA (la ND ya existe y está
-- conciliada; acá sólo cambian estados de factura y de cheque).
--
-- Neto BYG: facturas 26.977,86 → 5.342,77 ; cheques cartera 0 → 21.508,62.
-- (La dif 126,47 = retenciones/abonos parciales ya aplicados a esas facturas.)
--
-- IDEMPOTENTE: los WHERE piden estado ORIGEN (facturas vivas Z/A ; cheque
-- depositado B/A/I). Tras correr, re-ejecutar es no-op.
--
-- REVERSIBLE:
--   UPDATE scintela.factura SET stat='Z', saldo = importe - COALESCE(abono,0)
--    WHERE codigo_cli='BYG' AND numf IN (172265,172582,172799,174052,174269);
--   UPDATE scintela.cheque  SET stat='B'
--    WHERE codigo_cli='BYG' AND ROUND(importe::numeric,2)=21508.62 AND stat='1';
--
-- GUARD to_regclass OBLIGATORIO: no-op en test/CI donde scintela.* no existe.

DO $$
BEGIN
  IF to_regclass('scintela.factura') IS NULL
     OR to_regclass('scintela.cheque') IS NULL THEN
    RAISE NOTICE '0131: scintela.* ausente (CI/test) — no-op';
    RETURN;
  END IF;

  -- 1) Facturas de abril que pagó el cheque rebotado → T (sólo si siguen vivas)
  UPDATE scintela.factura
     SET stat = 'T', saldo = 0
   WHERE codigo_cli = 'BYG'
     AND numf IN (172265, 172582, 172799, 174052, 174269)
     AND COALESCE(stat, '') IN ('Z', 'A', '', ' ');

  -- 2) Cheque depositado que rebotó → estado 1 = "Devuelto" (relabel plano)
  UPDATE scintela.cheque
     SET stat = '1',
         usuario_modifica = 'mig-0131',
         fecha_modifica = CURRENT_TIMESTAMP
   WHERE codigo_cli = 'BYG'
     AND ROUND(importe::numeric, 2) = 21508.62
     AND COALESCE(stat, '') IN ('B', 'A', 'I');
END $$;
