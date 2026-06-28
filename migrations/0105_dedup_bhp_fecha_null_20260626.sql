-- 0105_dedup_bhp_fecha_null_20260626.sql
-- TMT 2026-06-26 (dueña: "los primeros cuatro movs se duplicaron, no puede
-- pasar"). El pendiente manual "KK SU CAJA (PENDIENTE)" se multiplicaba
-- (1→2→4) en cada "hacer prevalecer / cruzar".
--
-- CAUSA: el índice único anti-duplicado ux_bhp_firma era
--   (no_banco, fecha, COALESCE(documento,''), monto, tipo)
-- Coalesce sobre documento PERO NO sobre fecha. Los pendientes manuales
-- vienen con fecha NULL (y documento NULL). En Postgres NULL <> NULL en
-- índices únicos, así que (no_banco, NULL, '', 4042.43, 'C') nunca chocaba
-- consigo mismo → el ON CONFLICT DO NOTHING del INSERT (banco_v2_view.py
-- "hacer prevalecer") insertaba otra copia cada vez.
--
-- FIX: limpiar las copias existentes y recrear el índice con COALESCE(fecha).

BEGIN;

-- 1a) Borrar pendientes que duplican una fila YA conciliada (misma firma).
DELETE FROM scintela.banco_historicos_pendientes a
USING scintela.banco_historicos_pendientes b
WHERE a.no_banco = b.no_banco
  AND COALESCE(a.fecha, DATE '1900-01-01') = COALESCE(b.fecha, DATE '1900-01-01')
  AND COALESCE(a.documento, '') = COALESCE(b.documento, '')
  AND a.monto = b.monto
  AND a.tipo  = b.tipo
  AND a.conciliado_match_id IS NULL
  AND b.conciliado_match_id IS NOT NULL;

-- 1b) Entre pendientes idénticos (misma firma), conservar el id más bajo.
DELETE FROM scintela.banco_historicos_pendientes a
USING scintela.banco_historicos_pendientes b
WHERE a.no_banco = b.no_banco
  AND COALESCE(a.fecha, DATE '1900-01-01') = COALESCE(b.fecha, DATE '1900-01-01')
  AND COALESCE(a.documento, '') = COALESCE(b.documento, '')
  AND a.monto = b.monto
  AND a.tipo  = b.tipo
  AND a.conciliado_match_id IS NULL
  AND a.id > b.id;

-- 2) Recrear el índice único incluyendo COALESCE(fecha) — ahora las filas con
--    fecha NULL también deduplican.
DROP INDEX IF EXISTS scintela.ux_bhp_firma;
CREATE UNIQUE INDEX ux_bhp_firma
    ON scintela.banco_historicos_pendientes
       (no_banco, COALESCE(fecha, DATE '1900-01-01'), COALESCE(documento, ''), monto, tipo);

COMMIT;
