-- 0093_pichincha_residual_1a1_dbase.sql
-- TMT 2026-06-11 — cierre del residual Pichincha +8.586,89 vs dBase,
-- apareado 1 a 1 contra PICHINCH.DBF (tarball 10/06 16:44 EC).
--
-- Las 5 filas solo-dBase del 09/06 (GS IHE 155,44 · GS BANCO 127,21 ·
-- GS BED 568,51 · 1 ch.LMM 7.000 · 1 ch.LMM 989,78) ya se cargaron por
-- /bancos/nuevo-movimiento (flujo normal). Acá van las DOS correcciones
-- que no tienen pantalla de edición, cada una apareada a una fila exacta
-- del DBF, + el recompute del running saldo.
--
-- [1] 03/06 ND GS.BANCO: el dBase EDITÓ la fila a -905,81 después del
--     sync del 09/06; PC quedó con la versión vieja (-651,76).
--     Fila DBF: FECHA=2026-06-03 DOC=ND CONCEPTO=GS.BANCO IMPORTE=-905.81.
-- [2] ND 100,92 "Comisiones e impuestos 05/06-08/06" (08/06 en PC):
--     el dBase la asienta el 09/06 (ND GS BANCO 100,92). Solo se mueve
--     la fecha — importe y concepto quedan.
-- [3] Recompute saldo running Pichincha desde 03/06, anclado en la última
--     fila ANTERIOR al 03/06 (02/06 EOD = 2.338.395,90, verificado vs
--     dBase Δ=0,00). Además repara el corrimiento que dejaron los
--     recomputes disparados por los inserts backdated del 09/06:
--     recompute_saldos_desde(ancla_fecha) ancla con _saldo_previo SIN
--     excluir la fecha ancla → tomó como ancla la fila recién insertada
--     y desplazó la cadena un día-neto por cada insert (hero quedó en
--     462.916,76). Convención de delta IDÉNTICA a bank_helpers._signed_delta:
--     DE/TR/XX/NC/IN suman importe firmado, el resto lo resta.
--
-- IDEMPOTENTE: [1]/[2] con guards por fila exacta; [3] re-correrlo da el
-- mismo resultado. Esperado post-mig: última fila Pichincha = 2.071.012,63
-- (= PICHINCH.DBF), fin de día 03..10/06 Δ=0,00 contra el dBase.

DO $$
DECLARE
  v_id bigint;
BEGIN
  SELECT id_transaccion INTO v_id
    FROM scintela.transacciones_bancarias
   WHERE no_banco = 10
     AND fecha = DATE '2026-06-03'
     AND UPPER(TRIM(COALESCE(documento,''))) = 'ND'
     AND TRIM(COALESCE(concepto,'')) = 'GS.BANCO'
     AND ROUND(importe::numeric, 2) = -651.76;
  IF v_id IS NOT NULL THEN
    UPDATE scintela.transacciones_bancarias
       SET importe = -905.81,
           usuario_modifica = 'mig-0093',
           fecha_modifica = CURRENT_TIMESTAMP
     WHERE id_transaccion = v_id;
    RAISE NOTICE 'GS.BANCO 03/06: importe -651,76 -> -905,81 (id_transaccion %)', v_id;
  ELSE
    RAISE NOTICE 'GS.BANCO 03/06: ya corregida o no encontrada — no-op';
  END IF;

  SELECT id_transaccion INTO v_id
    FROM scintela.transacciones_bancarias
   WHERE no_banco = 10
     AND fecha = DATE '2026-06-08'
     AND UPPER(TRIM(COALESCE(documento,''))) = 'ND'
     AND TRIM(COALESCE(concepto,'')) LIKE 'Comisiones e impuestos%'
     AND ROUND(ABS(importe)::numeric, 2) = 100.92;
  IF v_id IS NOT NULL THEN
    UPDATE scintela.transacciones_bancarias
       SET fecha = DATE '2026-06-09',
           usuario_modifica = 'mig-0093',
           fecha_modifica = CURRENT_TIMESTAMP
     WHERE id_transaccion = v_id;
    UPDATE scintela.mov_doble
       SET fecha = DATE '2026-06-09'
     WHERE origen_table = 'transacciones_bancarias'
       AND origen_id = v_id;
    RAISE NOTICE 'ND 100,92: fecha 08/06 -> 09/06 (id_transaccion %)', v_id;
  ELSE
    RAISE NOTICE 'ND 100,92: ya en 09/06 o no encontrada — no-op';
  END IF;
END $$;

WITH anchor AS (
  SELECT COALESCE((
    SELECT saldo
      FROM scintela.transacciones_bancarias
     WHERE no_banco = 10
       AND fecha < DATE '2026-06-03'
       AND saldo IS NOT NULL
     ORDER BY fecha DESC, id_transaccion DESC
     LIMIT 1
  ), 0) AS s0
),
base AS (
  SELECT id_transaccion, fecha,
         CASE WHEN UPPER(TRIM(COALESCE(documento,''))) IN ('DE','TR','XX','NC','IN')
              THEN COALESCE(importe, 0)
              ELSE -COALESCE(importe, 0)
         END AS delta
    FROM scintela.transacciones_bancarias
   WHERE no_banco = 10
     AND fecha >= DATE '2026-06-03'
),
running AS (
  SELECT b.id_transaccion,
         ROUND(((SELECT s0 FROM anchor)
                + SUM(b.delta) OVER (ORDER BY b.fecha, b.id_transaccion
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
               )::numeric, 2) AS saldo_nuevo
    FROM base b
)
UPDATE scintela.transacciones_bancarias t
   SET saldo = r.saldo_nuevo
  FROM running r
 WHERE t.id_transaccion = r.id_transaccion
   AND t.saldo IS DISTINCT FROM r.saldo_nuevo;
