-- 0049_backfill_prov_desde_concepto.sql
-- TMT 2026-05-23 — backfill: para las tx con prov vacío o de 2 chars (que no
-- matchea con codigo_cli), extraer el código del concepto (regex "ch.XXX" /
-- "tr XXX" / "nc XXX") y popularlo si tiene 3-5 chars Y matchea con un
-- cliente real.
--
-- Antes: 30/902 txs Pichincha con prov poblado; 1/30 matchea cliente.
-- Después: esperamos que crezca significativamente.
--
-- IDEMPOTENTE: solo actualiza filas donde prov es NULL o vacío o tiene <3 chars.

BEGIN;

WITH cand AS (
    SELECT tb.id_transaccion,
           UPPER(TRIM((regexp_match(
               tb.concepto,
               '(?:^|\s)(?:\d+\s+)?(?:ch\.?|tr\.?|nc\.?|trf\.?|dep\.?\s*ch\.?)\s*([A-Za-z]{3,5})\b',
               'i'
           ))[1])) AS prov_extraido
      FROM scintela.transacciones_bancarias tb
     WHERE COALESCE(LENGTH(TRIM(tb.prov)), 0) < 3
       AND tb.concepto IS NOT NULL
),
validos AS (
    SELECT c.id_transaccion, c.prov_extraido
      FROM cand c
      JOIN scintela.cliente cli
        ON UPPER(TRIM(cli.codigo_cli)) = c.prov_extraido
     WHERE c.prov_extraido IS NOT NULL
)
UPDATE scintela.transacciones_bancarias tb
   SET prov = v.prov_extraido
  FROM validos v
 WHERE tb.id_transaccion = v.id_transaccion;

COMMIT;
