-- 0240_drop_procesa_provisiones.sql
--
-- Tamara 2026-09-03: se BORRA la procedure vieja `scintela.procesa_provisiones`.
--
-- Cargaba el MES COMPLETO de las 12 provisiones YY/RT de un saque (herencia
-- del dBase). Quedó redundante desde que `persistir_acumulacion_yy()`
-- (modules/posdat/queries.py) es el ÚNICO motor de devengo (decisión de la
-- dueña, 2026-06-10) — y el 01/09/2026 el cron del día 1 todavía la llamaba
-- encima del motor único: cada provisión se cargó DOS veces, 724.275 de más
-- (corregido por /admin/correccion-provisiones-doble/). El cron ya no la
-- llama y ningún archivo del repo la nombra como código (tests que barren el
-- repo en tests/test_procesa_provisiones_mensual.py), pero mientras exista en
-- la base un `CALL` a mano vuelve a duplicar. Sin la procedure, no hay cómo.
--
-- Se borran TODAS las sobrecargas por nombre (no se conoce la firma exacta
-- desde el repo: nunca se creó por migración). Idempotente: sin procedure,
-- no hace nada.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT p.oid::regprocedure AS firma, p.prokind
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'scintela'
           AND p.proname = 'procesa_provisiones'
    LOOP
        IF r.prokind = 'p' THEN
            EXECUTE 'DROP PROCEDURE ' || r.firma;
        ELSE
            EXECUTE 'DROP FUNCTION ' || r.firma;
        END IF;
        RAISE NOTICE 'borrada scintela.procesa_provisiones: %', r.firma;
    END LOOP;
END $$;
