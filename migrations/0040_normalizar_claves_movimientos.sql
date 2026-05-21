-- Migration 0040: normalizar claves de operador en tablas de movimiento.
--
-- Pedido dueña 2026-05-21: "hay algunos que dicen tel, otros TEL otros TAM".
-- Set canónico final = {FED, TAM, ALX, ADR} (Federico, Tamara, Alex, Andres).
-- ALE NO es válida — si aparece, queda en logs para revisión manual.
--
-- Tablas afectadas (sólo aquellas donde `clave` es el operador que registró
-- el movimiento, no la varchar(2) tipo-SRI de factura/xfactura, ni los
-- campos "clave" de cliente/tinto que tienen otra semántica):
--   scintela.caja, capital, cheque, compra, dolares, posdat, retiros,
--   transacciones_bancarias, xgast.
--
-- Transformaciones:
--   1) UPPER(clave) — colapsa 'tel'/'TEL'/'Tel' a 'TEL'.
--   2) Mapear legacy: 'TEL' → 'TAM' (operador Tamara); 'AND' → 'ADR' (Andres).
--   3) Idempotente: si ya está en {FED,TAM,ALX,ADR} no se toca.
--
-- Las que quedan fuera del set canónico (ALE, ALX, vacías raras) NO se
-- transforman — se reportan vía la vista `_claves_no_canonicas` que crea
-- esta migración. Querría: SELECT * FROM scintela._claves_no_canonicas;
--
-- Idempotente: re-correr la migración no rompe nada.

BEGIN;

-- 1) Helper function que normaliza una clave individual.
--    Aplica trim + upper + mapeo legacy.
CREATE OR REPLACE FUNCTION scintela._normalizar_clave(c text) RETURNS text AS $$
DECLARE
    u text;
BEGIN
    IF c IS NULL THEN
        RETURN NULL;
    END IF;
    u := UPPER(TRIM(c));
    IF u = '' THEN
        RETURN NULL;
    END IF;
    -- Mapeo legacy → canónico.
    IF u = 'TEL' THEN RETURN 'TAM'; END IF;  -- Tamara (legacy tel/TEL)
    IF u = 'AND' THEN RETURN 'ADR'; END IF;  -- Andres (renombrado)
    RETURN u;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 2) Aplicar la normalización en cada tabla de movimiento.
UPDATE scintela.caja
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.capital
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.cheque
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.compra
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.dolares
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.posdat
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.retiros
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.transacciones_bancarias
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

UPDATE scintela.xgast
   SET clave = scintela._normalizar_clave(clave)
 WHERE clave IS NOT NULL
   AND scintela._normalizar_clave(clave) IS DISTINCT FROM clave;

-- 3) Vista de auditoría: listar claves que NO están en el set canónico.
--    Útil para que la dueña vea si quedó algo raro (ej. 'ALE' o claves
--    de operarios viejos que ya no trabajan).
DROP VIEW IF EXISTS scintela._claves_no_canonicas;
CREATE VIEW scintela._claves_no_canonicas AS
WITH usos AS (
    SELECT 'caja' AS tabla, clave, COUNT(*) AS n FROM scintela.caja WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'capital', clave, COUNT(*) FROM scintela.capital WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'cheque', clave, COUNT(*) FROM scintela.cheque WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'compra', clave, COUNT(*) FROM scintela.compra WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'dolares', clave, COUNT(*) FROM scintela.dolares WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'posdat', clave, COUNT(*) FROM scintela.posdat WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'retiros', clave, COUNT(*) FROM scintela.retiros WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'transacciones_bancarias', clave, COUNT(*) FROM scintela.transacciones_bancarias WHERE clave IS NOT NULL GROUP BY clave
    UNION ALL
    SELECT 'xgast', clave, COUNT(*) FROM scintela.xgast WHERE clave IS NOT NULL GROUP BY clave
)
SELECT tabla, clave, n
  FROM usos
 WHERE clave NOT IN ('FED', 'TAM', 'ALX', 'ADR')
 ORDER BY tabla, n DESC;

COMMIT;
