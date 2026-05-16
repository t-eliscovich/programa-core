-- 0018_auto_saldo_trigger.sql
-- TMT 2026-05-11: "lo tiene que hacer solo".
--
-- BEFORE INSERT trigger en scintela.transacciones_bancarias que setea el
-- running `saldo` automáticamente si el caller no lo seteó explícitamente.
-- Hace que cualquier INSERT (legacy, raw, o vía bank_helpers) deje el
-- saldo bien sin tener que pasar por el botón "Recalcular saldos".
--
-- Idempotente: si ya existe el trigger/función, los reemplaza.

CREATE OR REPLACE FUNCTION scintela.fn_transacciones_bancarias_set_saldo()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  prev_saldo numeric(12,2);
  signo int;
BEGIN
  -- Si el caller ya seteó `saldo` (vía bank_helpers.insert_movimiento_bancario
  -- o un recompute), no lo pisamos.
  IF NEW.saldo IS NOT NULL THEN
    RETURN NEW;
  END IF;

  -- Signo del documento. Convención canónica del legacy BANCOS.PRG y de
  -- bank_helpers.DOCS_ENTRADA: DE/TR/XX/NC/IN suman, todo lo demás resta.
  signo := CASE
    WHEN UPPER(TRIM(COALESCE(NEW.documento, '')))
         IN ('DE','TR','XX','NC','IN') THEN  1
    ELSE -1
  END;

  -- Saldo anterior: última fila NO-NULL del mismo banco anterior a esta
  -- (por fecha + id_transaccion). Si el INSERT está al medio del ledger,
  -- las filas posteriores quedarán mal — para esos casos usar
  -- `bank_helpers.recompute_saldos_desde`. El flujo normal append-only
  -- (inserts al tail) no lo necesita.
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

  NEW.saldo := ROUND(
    COALESCE(prev_saldo, 0) + signo * ABS(COALESCE(NEW.importe, 0)),
    2
  );

  RETURN NEW;
END;
$$;


DROP TRIGGER IF EXISTS trg_transacciones_bancarias_set_saldo
  ON scintela.transacciones_bancarias;

CREATE TRIGGER trg_transacciones_bancarias_set_saldo
BEFORE INSERT ON scintela.transacciones_bancarias
FOR EACH ROW
EXECUTE FUNCTION scintela.fn_transacciones_bancarias_set_saldo();
