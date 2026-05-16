-- migrate:no-transaction
-- Migration 0030: redo migration 0029 indices using CONCURRENTLY.
--
-- Contexto (TMT 2026-05-15, re-audit L1): la 0029 usó `CREATE INDEX IF NOT
-- EXISTS` plain, que toma ACCESS EXCLUSIVE LOCK sobre la tabla durante toda
-- la creación. En local con DBs chicas es invisible; en producción (EC2
-- con scintela.transacciones_bancarias hot, scintela.chequesxfact con miles
-- de filas, scintela.cheque con histórico de años) bloqueaba reads/writes
-- al subir.
--
-- Esta migración:
--   1. DROP de los índices viejos (sin CONCURRENTLY → quick, ya existen).
--   2. CREATE INDEX CONCURRENTLY de los mismos índices.
--
-- CONCURRENTLY no puede correr dentro de una transacción. El runner detecta
-- el header `-- migrate:no-transaction` en la primera línea y cambia a
-- autocommit + statement-por-statement antes de ejecutar.
--
-- IMPORTANTE: idempotente — `IF NOT EXISTS` en el CREATE, `IF EXISTS` en
-- el DROP. Se puede correr varias veces sin romper. Si el runner falla a
-- mitad de camino vas a ver un índice INVALID en pg_indexes (indisvalid=
-- false); droppealo y dale de nuevo.

-- ITEM #7 — id_cheque_padre (parcial).
DROP INDEX IF EXISTS scintela.cheque_id_cheque_padre_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS cheque_id_cheque_padre_idx
    ON scintela.cheque (id_cheque_padre)
    WHERE id_cheque_padre IS NOT NULL;

COMMENT ON INDEX scintela.cheque_id_cheque_padre_idx IS
    'Índice parcial para acelerar lookup de cheques hijos '
    '(espejos de anticipo + reemplazos XX). Recreado CONCURRENTLY '
    'por la migration 0030 — la versión 0029 era no-concurrent y '
    'bloqueaba reads en deploy.';


-- ITEM #8 — transacciones_bancarias compuesto.
DROP INDEX IF EXISTS scintela.tx_bancarias_fecha_doc_banco_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS tx_bancarias_fecha_doc_banco_idx
    ON scintela.transacciones_bancarias (fecha, documento, no_banco);

COMMENT ON INDEX scintela.tx_bancarias_fecha_doc_banco_idx IS
    'Índice compuesto (fecha, documento, no_banco) para boleta_deposito + '
    'cobros_matriz_3_semanas. Recreado CONCURRENTLY por la 0030 — la 0029 '
    'bloqueaba la tabla más hot del sistema durante el deploy.';


-- ITEM #10 — chequesxfact.fechaing.
DROP INDEX IF EXISTS scintela.chequesxfact_fechaing_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS chequesxfact_fechaing_idx
    ON scintela.chequesxfact (fechaing);

COMMENT ON INDEX scintela.chequesxfact_fechaing_idx IS
    'Índice sobre fechaing para matrices/agendas de cobros. Recreado '
    'CONCURRENTLY por la 0030.';


-- Nota operativa para el próximo deploy:
--   Si por alguna razón el runner falla a mitad de camino, vas a ver un
--   índice "INVALID" en pg_indexes (col `indisvalid=false`). Para limpiarlo:
--       DROP INDEX scintela.<nombre>_idx;
--       CREATE INDEX CONCURRENTLY <nombre>_idx ...
--   PostgreSQL nunca usa índices invalid para queries, así que la pérdida
--   es sólo de performance, no de correctitud.
