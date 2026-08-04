-- Migración 0157: el candado sin excepciones. Cierre del tema.
--
-- TMT 2026-08-04, al final del día. La 0155 prohibió repetir `codigo_cli` y
-- dejó 7 fichas exceptuadas — los pares que todavía necesitaban una decisión.
-- La 0156 achicó esa lista a 3. Hoy la dueña resolvió los 3 últimos (LEC,
-- BLP, BRC) y **no queda ningún código repetido**: 20 → 0.
--
-- Esta migración saca la lista de excepción del índice. Desde acá el candado
-- cubre `scintela.cliente` **entera**, sin agujeros.
--
-- Por qué no se re-corre la 0156 y listo: `scripts/migrate.py` sólo aplica lo
-- PENDIENTE, y re-aplicar a mano es `--force`, que se corre por consola en el
-- EC2 — justo lo que `operar-por-la-ui` evita. Una migración nueva se aplica
-- desde `/admin/migraciones`, que es una pantalla.
--
-- El guard es el mismo de siempre pero al revés: en vez de recalcular la
-- excepción, **exige que no haga falta ninguna**. Si quedara un código
-- repetido, corta diciendo cuál y no toca el índice — el de la 0156, que
-- todavía lo protege, sigue en pie.
--
-- Idempotente.

DO $$
DECLARE
    v_repetidos TEXT;
BEGIN
    SELECT STRING_AGG(q.cod || ' (' || q.n || ' fichas)', ', ' ORDER BY q.cod)
      INTO v_repetidos
      FROM (
        SELECT UPPER(TRIM(c.codigo_cli)) AS cod, COUNT(*) AS n
          FROM scintela.cliente c
         WHERE COALESCE(TRIM(c.codigo_cli), '') <> ''
         GROUP BY UPPER(TRIM(c.codigo_cli))
        HAVING COUNT(*) > 1
      ) q;

    IF v_repetidos IS NOT NULL THEN
        RAISE EXCEPTION
            'Todavía hay códigos repetidos: %. No saco la lista de excepción — '
            'el índice de la 0156 los sigue tapando. Resolvelos primero desde '
            '/admin/clientes-asinfo.', v_repetidos;
    END IF;

    DROP INDEX IF EXISTS scintela.cliente_codigo_cli_unico;

    -- Sin `NOT IN`. Los códigos VACÍOS siguen afuera: hay clientes legacy sin
    -- código y no queremos que exista un único cliente-sin-código.
    CREATE UNIQUE INDEX cliente_codigo_cli_unico
        ON scintela.cliente (UPPER(TRIM(codigo_cli)))
     WHERE COALESCE(TRIM(codigo_cli), '') <> '';

    RAISE NOTICE 'Índice sin excepciones: ningún código de cliente se puede repetir.';
END
$$;

COMMENT ON INDEX scintela.cliente_codigo_cli_unico IS
    'Un código de cliente no se puede repetir (TMT 2026-08-04). Sin '
    'excepciones desde la 0157: los 20 códigos duplicados que había quedaron '
    'todos resueltos.';
