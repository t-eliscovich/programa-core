-- Migration 0120: marcar como INACTIVOS los vendedores que "no van".
--
-- CONTEXTO (dueña 2026-07-09): en "Estados de cuenta → Vendedor" aparecían
-- códigos que NO son vendedores reales (BED, DJA, JLZ, CEM, …). Esos códigos
-- entraron a scintela.vendedor por el backfill de la migración 0032 (que
-- insertó todo UPPER(TRIM(cliente.vend)) distinto), pero los vendedores
-- OFICIALES son sólo 6 (dueña 2026-05-19, migración 0034):
--     PPR, EDG, SEP, JQU, FL1, RMY.
--
-- Marcamos activo=FALSE a todo lo que no sea uno de esos 6. La vista de
-- estados de cuenta por vendedor filtra por activo=TRUE, así que los códigos
-- que "no van" desaparecen del selector; sus clientes caen en "(sin vendedor)"
-- (no se pierden). Idempotente: sólo cambia los que hoy están en TRUE.
--
-- Si la dueña quiere reactivar/agregar un vendedor, se hace por la fila
-- (activo=TRUE) — no hay que tocar código.

UPDATE scintela.vendedor
   SET activo = FALSE
 WHERE COALESCE(activo, TRUE) = TRUE
   AND UPPER(TRIM(codigo)) NOT IN ('PPR', 'EDG', 'SEP', 'JQU', 'FL1', 'RMY');
