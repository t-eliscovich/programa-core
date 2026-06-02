-- 0065_sesion_saldo_objetivo.sql
-- Saldo banco objetivo manual — TMT 2026-06-02.
--
-- Dueña: 'saldo al que quiero llegar del banco, al final en que quedo?
-- lo deberia implementar el usuario no? porque por el excel no sabemos
-- cual es el ultimo valor'.
--
-- Hasta hoy: saldo_banco_real se autodetectaba como max(fecha).saldo del
-- payload de la sesión. Frágil: si hay varios movs el mismo día o varios
-- uploads merged, el max no es determinístico.
--
-- Ahora: la dueña escribe el saldo que el banco reporta (lo lee del xlsx
-- o de la web del banco). Si está vacío, caemos al auto-detect anterior.

ALTER TABLE scintela.banco_conciliacion_sesion
    ADD COLUMN IF NOT EXISTS saldo_banco_objetivo NUMERIC(18, 2);

COMMENT ON COLUMN scintela.banco_conciliacion_sesion.saldo_banco_objetivo IS
    'Saldo al cierre que el banco reporta. Manual. Si NULL, se autodetecta del payload.';
