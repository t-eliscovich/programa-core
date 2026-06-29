-- 0106_sesion_saldo_detectado_20260626.sql
-- TMT 2026-06-26 (dueña: "el saldo objetivo lo tomó mal, debería ser
-- 2.822.126,77 y no 2.815.880,63").
--
-- El auto-detect del saldo banco objetivo leía SOLO el extracto de la sesión
-- (cargar_movs) y tomaba max(fecha) con desempate arbitrario entre movs del
-- mismo día. Peor: los movs más nuevos (26/06) se deduplicaban a históricos
-- y NO quedaban en la sesión, así que la fecha máx de la sesión era 25/06 y
-- agarraba un saldo del medio del día.
--
-- FIX: guardamos en la sesión el saldo REAL del extracto (saldo de la fila
-- más nueva), calculado al subir el archivo ANTES del dedup. El auto-detect
-- usa este valor (prioridad: objetivo manual > detectado > viejo auto).

ALTER TABLE scintela.banco_conciliacion_sesion
    ADD COLUMN IF NOT EXISTS saldo_banco_detectado NUMERIC(14,2);
