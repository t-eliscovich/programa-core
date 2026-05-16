-- 0021_trigger_only_no_backfill.sql
-- TMT 2026-05-11: 0019 + 0020 sobrescribieron los saldos legacy con un
-- running calculado, pero al partir de 0 (sin opening balance) los
-- saldos quedaron negativos. La verdad: el dBase trae los saldos como
-- snapshot ya correcto en el campo `saldo` del DBF — hay que confiar
-- en eso para las filas legacy, NO recalcular.
--
-- Esta migración:
--   1) Asegura el trigger smart-delta (idempotente). Sirve para que
--      cualquier INSERT futuro autocalcule su saldo a partir del
--      previo + delta_firmado. No toca las filas existentes.
--   2) NO hace backfill — preserva lo que la DB tenga.
--
-- Para arreglar los saldos rotos de Pichincha/Internacional hace falta
-- re-correr el import del DBF de bancos (los DBF tienen el running
-- saldo legacy ya almacenado):
--
--     python scripts/import_dbf.py --only=PICHINCH.DBF,INTER.DBF
--
-- Después de re-importar, los saldos vuelven a los del dBase y el
-- trigger sigue tomando los siguientes INSERTs correctamente.

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
  -- Si el caller ya seteó saldo explícito, no lo pisamos.
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
