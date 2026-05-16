-- 0022_auto_saldo_trigger_caja.sql
-- TMT 2026-05-11 (audit): el mismo problema que tenía
-- transacciones_bancarias (saldo NULL después de INSERTs raw) lo tiene
-- también scintela.caja. Ejemplo: transferencia banco→caja desde el
-- form de emitir cheque (modules/bancos/queries.py línea 213) hace un
-- INSERT raw sin saldo. La fila queda con saldo NULL.
--
-- Esta migración: trigger BEFORE INSERT en scintela.caja que setea el
-- running saldo automático. Misma idea que 0021 para banco.
--
-- Convención de signo en caja (legacy dBase):
--   tipo 'E' (entrada)  → +importe
--   tipo 'S' (salida)   → -importe
--   tipo 'CB' (cobro)   → +importe (entrada en efectivo)
--   importe se guarda SIEMPRE en valor absoluto (es la convención del
--   módulo caja_helpers).
--
-- Idempotente.

DROP TRIGGER IF EXISTS trg_caja_set_saldo ON scintela.caja;
DROP FUNCTION IF EXISTS scintela.fn_caja_set_saldo();

CREATE OR REPLACE FUNCTION scintela.fn_caja_set_saldo()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  prev_saldo numeric(12,2);
  delta numeric(12,2);
  signo int;
BEGIN
  -- Respetar saldo explícito del caller (caja_helpers ya lo computa).
  IF NEW.saldo IS NOT NULL THEN
    RETURN NEW;
  END IF;

  -- Signo por tipo. 'S' es egreso (-), todo lo demás suma (+).
  signo := CASE
    WHEN UPPER(TRIM(COALESCE(NEW.tipo, ''))) = 'S' THEN -1
    ELSE 1
  END;

  delta := signo * ABS(COALESCE(NEW.importe, 0));

  -- Saldo anterior: última fila NO-NULL antes de esta (por fecha + id).
  SELECT saldo INTO prev_saldo
    FROM scintela.caja
   WHERE saldo IS NOT NULL
     AND (
       (fecha < NEW.fecha)
       OR (fecha = NEW.fecha
           AND (NEW.id_caja IS NULL OR id_caja < NEW.id_caja))
     )
   ORDER BY fecha DESC, id_caja DESC
   LIMIT 1;

  NEW.saldo := ROUND(COALESCE(prev_saldo, 0) + delta, 2);
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_caja_set_saldo
BEFORE INSERT ON scintela.caja
FOR EACH ROW
EXECUTE FUNCTION scintela.fn_caja_set_saldo();
