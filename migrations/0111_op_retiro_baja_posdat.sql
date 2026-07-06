-- 0111_op_retiro_baja_posdat.sql
-- TMT 2026-07-06 (dueña): "bajar la deuda OP de verdad". El retiro OP ahora
-- EDITA la fila posdat OP (importe += monto, el crédito negativo sube hacia 0)
-- → Pasivos en Resultados SÍ se mueve. El retiro sigue yendo a scintela.retiros
-- (URET sube) → "Ut. Real" (ΔPATR + URET) queda quieta: se COMPENSA, como
-- pidió la dueña ("pero sube retiros, debería compensar"). La utilidad del mes
-- (PATR−PATANT) baja hasta el próximo cierre, igual que cualquier retiro.
--
-- bajo_posdat marca qué imputaciones BAJARON el posdat (las nuevas). Las
-- viejas (false) eran display-only: el deshacer NO les devuelve importe al
-- posdat, y el "restante" de la fila las sigue restando del display.
-- Idempotente.
ALTER TABLE scintela.op_retiro_linea
  ADD COLUMN IF NOT EXISTS bajo_posdat BOOLEAN NOT NULL DEFAULT FALSE;
