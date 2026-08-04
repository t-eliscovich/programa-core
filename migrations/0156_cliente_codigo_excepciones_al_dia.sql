-- Migración 0156: poner al día la lista de excepción del índice único.
--
-- TMT 2026-08-04. La 0155 creó `cliente_codigo_cli_unico` con 7 ids
-- exceptuados — uno por cada par que todavía no se había resuelto. Ese día
-- quedó escrito que la lista **se achica, no crece**, y que sacar un id
-- pedía una migración nueva. Ésta es esa migración, pero hecha una sola vez
-- y para siempre: en vez de tipear la lista corta a mano cada vez, la
-- **recalcula** a partir de lo que hoy sigue duplicado de verdad.
--
-- Por qué recalcular es seguro acá y no lo sería en general: **el índice ya
-- impide crear duplicados nuevos**. Así que el conjunto de códigos repetidos
-- sólo puede encogerse. Una lista auto-calculada que sólo puede encoger no
-- es una puerta trasera — es la misma lista explícita, sin el paso manual
-- que se olvida.
--
-- Y por si esa premisa alguna vez deja de valer (alguien dropea el índice,
-- una carga masiva entra por otro lado), el guard lo chequea: si aparece un
-- id que **no** estaba en la lista de la 0155, corta con `RAISE EXCEPTION` y
-- no toca nada. Un duplicado nuevo tiene que doler, no colarse adentro de
-- una excepción.
--
-- Idempotente: recalcula, y si la lista no cambió recrea el mismo índice.

DO $$
DECLARE
    -- La lista tal cual quedó en la 0155. Es el techo: nada que no esté acá
    -- puede aparecer en la lista nueva.
    v_0155     INTEGER[] := ARRAY[29142, 30547, 31469, 29048, 31038, 32066, 31562];
    v_nueva    INTEGER[];
    v_intrusos INTEGER[];
    v_ddl      TEXT;
BEGIN
    -- Los ids que HOY siguen necesitando excepción: de cada código repetido,
    -- todas las fichas menos la primera. Con la primera indexada alcanza para
    -- que un código TERCERO que se repita explote igual.
    SELECT COALESCE(ARRAY_AGG(q.id_cliente ORDER BY q.id_cliente), ARRAY[]::INTEGER[])
      INTO v_nueva
      FROM (
        SELECT c.id_cliente,
               ROW_NUMBER() OVER (PARTITION BY UPPER(TRIM(c.codigo_cli))
                                      ORDER BY c.id_cliente) AS rn,
               COUNT(*)     OVER (PARTITION BY UPPER(TRIM(c.codigo_cli))) AS n
          FROM scintela.cliente c
         WHERE COALESCE(TRIM(c.codigo_cli), '') <> ''
      ) q
     WHERE q.n > 1 AND q.rn > 1;

    -- El guard: nada nuevo puede entrar a la lista.
    SELECT COALESCE(ARRAY_AGG(x), ARRAY[]::INTEGER[])
      INTO v_intrusos
      FROM UNNEST(v_nueva) AS x
     WHERE NOT (x = ANY (v_0155));

    IF ARRAY_LENGTH(v_intrusos, 1) > 0 THEN
        RAISE EXCEPTION
            'Hay códigos repetidos NUEVOS (fichas %). El índice único tendría '
            'que haberlo impedido: revisá si sigue existiendo antes de seguir. '
            'No toqué nada.',
            ARRAY_TO_STRING(v_intrusos, ', ');
    END IF;

    DROP INDEX IF EXISTS scintela.cliente_codigo_cli_unico;

    -- El DDL va dollar-quoted ($ddl$) y no entre comillas simples: el
    -- predicado lleva `<> ''` adentro, y escaparlo a mano dentro de un
    -- literal de PL/pgSQL pide OCHO comillas seguidas. Ya se escribió mal una
    -- vez —quedó `<> ''''`, o sea "distinto de una comilla"— y con eso los
    -- códigos VACÍOS entraban al índice y chocaban entre ellos. El
    -- dollar-quoting hace que lo que se lee sea exactamente lo que corre.
    IF CARDINALITY(v_nueva) > 0 THEN
        v_ddl := FORMAT($ddl$
            CREATE UNIQUE INDEX cliente_codigo_cli_unico
                ON scintela.cliente (UPPER(TRIM(codigo_cli)))
             WHERE COALESCE(TRIM(codigo_cli), '') <> ''
               AND id_cliente NOT IN (%s)
        $ddl$, ARRAY_TO_STRING(v_nueva, ', '));
        RAISE NOTICE 'Códigos repetidos que siguen abiertos: % (fichas %)',
            CARDINALITY(v_nueva), ARRAY_TO_STRING(v_nueva, ', ');
    ELSE
        -- El caso que buscábamos: ni una excepción. El candado cubre la
        -- tabla entera.
        v_ddl := $ddl$
            CREATE UNIQUE INDEX cliente_codigo_cli_unico
                ON scintela.cliente (UPPER(TRIM(codigo_cli)))
             WHERE COALESCE(TRIM(codigo_cli), '') <> ''
        $ddl$;
        RAISE NOTICE 'No queda ningún código repetido: el índice ya no tiene excepciones.';
    END IF;

    EXECUTE v_ddl;
END
$$;

COMMENT ON INDEX scintela.cliente_codigo_cli_unico IS
    'Un código de cliente no se puede repetir (TMT 2026-08-04). Las '
    'excepciones se recalculan con la migración 0156 a partir de lo que '
    'sigue duplicado; sólo pueden achicarse.';
