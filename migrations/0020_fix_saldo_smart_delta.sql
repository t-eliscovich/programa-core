-- 0020_fix_saldo_smart_delta.sql
-- TMT 2026-05-11: la migración 0019 rompió los saldos porque trataba todos
-- los importes como ABS. La verdad: la columna `importe` tiene CONVENCIÓN
-- MIXTA en esta DB.
--
--   - Filas DBF legacy:   importe SIGNED  (-N para ND/CH/egresos, +N para DE)
--   - Filas bank_helpers: importe ABS     (+N siempre, signo vive en `documento`)
--
-- La regla "smart delta" maneja ambos casos:
--
--     IF importe < 0  → delta = importe     (legacy ya firmado)
--     ELSE IF doc IN ('DE','TR','XX','NC','IN') → delta = +importe   (entrada)
--     ELSE → delta = -importe                                        (egreso)
--
-- Esta migración:
--   1) Reemplaza el trigger de 0018 con la nueva fórmula.
--   2) Re-backfilla todos los saldos. Equivale a deshacer el daño de 0019.

-- ────────────────────────────────────────────────────────────────────
-- 1) Trigger con smart delta — reemplaza el de 0018
-- ────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_transacciones_bancarias_set_saldo
  ON scintela.transacciones_bancarias;
DROP FUNCTION IF EXISTS scintela.fn_transacciones_bancarias_set_saldo();

CREATE OR REPLACE FUNCTION scintela.fn_transacciones_bancarias_set_saldo()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  prev_saldo numeric(12,2);
  delta numeric(12,2);
BEGIN
  -- Si el caller seteó saldo explícito, no lo pisamos.
  IF NEW.saldo IS NOT NULL THEN
    RETURN NEW;
  END IF;

  -- Smart delta: respeta importes signed legacy + signa los abs nuevos.
  IF COALESCE(NEW.importe, 0) < 0 THEN
    delta := NEW.importe;
  ELSIF UPPER(TRIM(COALESCE(NEW.documento, '')))
       IN ('DE','TR','XX','NC','IN') THEN
    delta := COALESCE(NEW.importe, 0);
  ELSE
    delta := -COALESCE(NEW.importe, 0);
  END IF;

  -- Saldo anterior: última fila NO-NULL del mismo banco antes de esta.
  SELECT saldo INTO prev_saldo
    FROM scintela.transacciones_bancarias
   WHERE no_banco = NEW.no_banco
     AND saldo IS NOT NULL
     AND (
       (fecha < NEW.fecha)
       OR (fecha = NEW.fecha
           AND (NEW.id_transaccion IS NULL
                OR id_transaccion < NEW.id_transaccion))
     )
   ORDER BY fecha DESC, id_transaccion DESC
   LIMIT 1;

  NEW.saldo := ROUND(COALESCE(prev_saldo, 0) + delta, 2);
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_transacciones_bancarias_set_saldo
BEFORE INSERT ON scintela.transacciones_bancarias
FOR EACH ROW
EXECUTE FUNCTION scintela.fn_transacciones_bancarias_set_saldo();


-- ────────────────────────────────────────────────────────────────────
-- 2) Re-backfill TODOS los saldos con la regla correcta.
--    Window function: para cada (no_banco, no_cta), suma running.
-- ────────────────────────────────────────────────────────────────────
WITH running AS (
  SELECT
    id_transaccion,
    SUM(
      CASE
        WHEN COALESCE(importe, 0) < 0 THEN importe
        WHEN UPPER(TRIM(COALESCE(documento, '')))
             IN ('DE','TR','XX','NC','IN') THEN COALESCE(importe, 0)
        ELSE -COALESCE(importe, 0)
      END
    ) OVER (
      PARTITION BY no_banco, COALESCE(no_cta, '')
      ORDER BY fecha, id_transaccion
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS saldo_calculado
  FROM scintela.transacciones_bancarias
)
UPDATE scintela.transacciones_bancarias t
   SET saldo = ROUND(r.saldo_calculado::numeric, 2),
       fecha_modifica = CURRENT_TIMESTAMP,
       usuario_modifica = COALESCE(t.usuario_modifica, 'migration-0020')
  FROM running r
 WHERE t.id_transaccion = r.id_transaccion;
