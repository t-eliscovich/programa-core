-- 0131_byg_cheque_devuelto_opcion1.sql
-- TMT 2026-07-25 — Cheques REBOTADOS: replicar EXACTO el dBase.
--
-- Decisión de la dueña: "dejalo como queda el dBase". El dBase, cuando un
-- cheque rebota, lo deja en STAT=V (que NO cuenta en cartera: TOTC = STAT $
-- 'Z123PD', sin V) y las facturas que ese cheque había pagado quedan en T.
-- La deuda SALE de cartera; el único rastro es la nota de débito en el banco.
--
-- LOS 11 CHEQUES REBOTADOS (dBase CHEQUES.DBF STAT=V, verificados contra el
-- tarball 24/07): BYG 21508.62, EDU 20000, AJ2 9315.21, LRA 2332 (x2),
-- OPM 2226 + 139, FAT 1680.65, FLA 1275.83, AAM 962.09, CG3 879.96.
-- En PC quedaron como DEPOSITADOS (stat B) — el sync no los pasó a devuelto.
-- La ND del banco YA existe y está CONCILIADA (fix 23/07). Acá NO se toca el
-- banco → la conciliación queda intacta.
--
-- QUÉ HACE:
--   1. Cheques rebotados que hoy están depositados (B/A/I) → V. V no cuenta en
--      TOTC (igual que el dBase), y en PC V significa lo mismo (protestado/
--      devuelto). Como B tampoco cuenta, pasar B→V NO cambia el total —
--      formaliza el estado devuelto y arregla la etiqueta. Match cliente+importe.
--   2. BYG: las 5 facturas de abril que pagó su cheque (172265, 172582, 172799,
--      174052, 174269) → T (saldo 0). En PC estaban vivas e inflaban facturas
--      +21.635. Con esto BYG cuadra EXACTO con el dBase (facturas 5.342,77,
--      cheque V no cuenta). La 172799 ya se pasó a T a mano el 25/07 (guard la
--      saltea).
--   (Otros clientes: sus facturas del rebote ya están en T en PC o su Δ es de
--    retenciones — no se tocan acá; sólo el cheque → V.)
--
-- IDEMPOTENTE: guards por estado origen. REVERSIBLE:
--   UPDATE scintela.factura SET stat='Z', saldo=importe-COALESCE(abono,0)
--    WHERE codigo_cli='BYG' AND numf IN (172265,172582,172799,174052,174269);
--   UPDATE scintela.cheque SET stat='B' WHERE stat='V' AND usuario_modifica='mig-0131';
--
-- GUARD to_regclass OBLIGATORIO: no-op en test/CI donde scintela.* no existe.

DO $$
BEGIN
  IF to_regclass('scintela.factura') IS NULL
     OR to_regclass('scintela.cheque') IS NULL THEN
    RAISE NOTICE '0131: scintela.* ausente (CI/test) — no-op';
    RETURN;
  END IF;

  -- 1) BYG: facturas de abril que pagó el cheque rebotado → T (sólo si vivas)
  UPDATE scintela.factura
     SET stat = 'T', saldo = 0
   WHERE codigo_cli = 'BYG'
     AND numf IN (172265, 172582, 172799, 174052, 174269)
     AND COALESCE(stat, '') IN ('Z', 'A', '', ' ');

  -- 2) Los 11 cheques rebotados que hoy están depositados → V (como el dBase)
  UPDATE scintela.cheque
     SET stat = 'V',
         usuario_modifica = 'mig-0131',
         fecha_modifica = CURRENT_TIMESTAMP
   WHERE COALESCE(stat, '') IN ('B', 'A', 'I')
     AND (UPPER(TRIM(codigo_cli)), ROUND(importe::numeric, 2)) IN (
       ('BYG', 21508.62), ('EDU', 20000.00), ('AJ2', 9315.21),
       ('LRA', 2332.00), ('OPM', 2226.00), ('OPM', 139.00),
       ('FAT', 1680.65), ('FLA', 1275.83), ('AAM', 962.09),
       ('CG3', 879.96)
     );
END $$;
