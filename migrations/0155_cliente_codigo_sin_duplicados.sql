-- Migración 0155: PROHIBIR los códigos de cliente duplicados.
--
-- TMT 2026-08-04 (dueña): "hay que prohibir codigos duplicados".
--
-- ⭐ POR QUÉ
-- ---------
-- Todo el sistema JOINea por `codigo_cli` — la factura, el cheque, la
-- retención y el cobro guardan el STRING de 3 letras, no `id_cliente`. Un
-- código repetido, entonces, no es un problema cosmético: **duplica la
-- plata**. Ya pasó dos veces y las dos costaron una tarde:
--   · la comisión de un vendedor se infló $4.341,86 (julio 2026);
--   · el estado de cuenta de GUF mostró el mismo saldo 15.104,31 dos veces
--     (03/08) porque había dos fichas con ese código.
--
-- `scintela.cliente` tenía **20 códigos repetidos**. La hipótesis de trabajo
-- —la que motivó `/clientes/<id>/cambiar-codigo`— era que siempre son dos
-- empresas distintas chocando, porque el código son las INICIALES del nombre
-- (`LEC` = Luis Ernesto Cañamar **y** Lola Emperatriz Cisneros). Es cierto en
-- 6 de los 20. En los otros 14 el patrón es OTRO: la ficha con el código
-- bueno **ya existe en PC, con el mismo RUC**.
--
--     ficha 29243  ALP  ALEXA MAGDALENA PINCAY ESPIN   1202632590001
--     ficha 30021  AMP  ALEXA MAGDALENA PINCAY         1202632590001  ← ya está
--
-- No es un cliente mal codificado: es el **mismo cliente cargado dos veces**.
-- Ahí renombrar ni siquiera entra (`plan_cambio_codigo` rechaza el destino
-- ocupado, y hace bien). La acción es borrar la fila sobrante.
--
-- Esta migración hace las dos cosas, en una sola transacción:
--   1) borra las 15 fichas sobrantes (13 códigos), con un guard que vuelve a
--      exigir en RUNTIME los dos hechos que hacen segura cada baja;
--   2) crea el índice único que a partir de ahora **impide** que un código se
--      repita.
--
-- QUÉ NO HACE: no toca los 7 códigos que siguen abiertos (BLP, BRC, JQS,
-- LEC, YMO, JRP, MTE). Ésos son dos empresas REALES cada uno y la decisión es
-- de la dueña — están en la lista de excepción del índice, que es explícita y
-- se ACHICA a medida que se resuelven. No es una puerta trasera: mientras
-- tanto el índice ya protege a los otros ~3.800.
--
-- POR QUÉ EL GUARD, si la lista salió de una revisión manual ficha por ficha:
-- porque entre la revisión y el día en que esto corra en producción alguien
-- pudo facturarle a una de estas fichas, o borrar su hermana. La lista dice
-- "revisá estas 15"; el guard dice "y sólo si SIGUEN cumpliendo". Si alguna
-- no cumple, `RAISE EXCEPTION` con el número de ficha y **no se aplica nada**
-- (una sola transacción). Se corta acá y no en el `CREATE INDEX` porque el
-- error de Postgres por índice duplicado no dice cuál ficha ni por qué.
--
-- Idempotente: en la segunda corrida las 15 ya no existen (se saltean, 0
-- borradas) y el índice ya está (`IF NOT EXISTS`).

DO $$
DECLARE
    -- Las 15 fichas sobrantes. Al lado, el código que tienen hoy y el código
    -- con el que el MISMO cliente (mismo RUC) ya está cargado en PC.
    --                                     código   ya existe como
    v_condenadas INTEGER[] := ARRAY[
        29243,  -- ALP  ALEXA MAGDALENA PINCAY ESPIN       → AMP
        29347,  -- BEH  BENJAMIN HERNANDEZ                 → BHE
        31346,  -- BSA  ASOPROTEXCOCI BERONICA SALAZAR     → BES
        31994,  -- DIC  DUMANCELA IBARRA CARMEN GRACIELA   → GCD
        32143,  -- FSB  FRANK ESTEBAN BONILLA POZO         → FBP
        30082,  -- JAQ  JAQUELINE CARRASCO                 → JMC
        31453,  -- JIL  ASOPROTEXPROMAN JILVER LOOR        → GIL
        29390,  -- KAB  KATHY BRITO                        → KBR
        29483,  -- KAB  KAREN ALEJANDRA BENAVIDES          → KBF
        29191,  -- LUL  LUIS LOPEZ                         → LLO
        29173,  -- NOB  NORA BARRIONUEVO                   → NHB
        29336,  -- NOB  NORMANDY BENALCAZAR                → NBE
        29581,  -- NUS  NORMA ISAURA ALBA USHIÑA           → NOA
        32295,  -- APD  ALMACENES JOSE PUEBLA 2            → AJ2 (mismo RUC y misma dirección)
        31707   -- YEL  MARIBEL YESSENIA LEON HERRERA      → gemela exacta de 29950
    ];
    v_id        INTEGER;
    v_cod       TEXT;
    v_ruc10     TEXT;
    v_movs      BIGINT;
    v_proformas BIGINT;
    v_hermanas  BIGINT;
    v_problemas TEXT[] := ARRAY[]::TEXT[];
    v_borradas  INTEGER := 0;
BEGIN
    FOREACH v_id IN ARRAY v_condenadas LOOP
        SELECT UPPER(TRIM(COALESCE(c.codigo_cli, ''))),
               LEFT(REGEXP_REPLACE(COALESCE(c.ruc, ''), '[^0-9]', '', 'g'), 10)
          INTO v_cod, v_ruc10
          FROM scintela.cliente c
         WHERE c.id_cliente = v_id;

        -- Ya no está: segunda corrida, o alguien la borró a mano. No es un
        -- error — el estado final buscado es exactamente ése.
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        -- (1) Ni un solo movimiento colgando de su CÓDIGO. Si tiene, borrar la
        --     ficha deja plata sin dueño identificable.
        SELECT (SELECT COUNT(*) FROM scintela.factura      f WHERE UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = v_cod)
             + (SELECT COUNT(*) FROM scintela.cheque       q WHERE UPPER(TRIM(COALESCE(q.codigo_cli, ''))) = v_cod)
             + (SELECT COUNT(*) FROM scintela.chequesxfact x WHERE UPPER(TRIM(COALESCE(x.codigo_cli, ''))) = v_cod)
             + (SELECT COUNT(*) FROM scintela.retencion    r WHERE UPPER(TRIM(COALESCE(r.codigo_cli, ''))) = v_cod)
             + (SELECT COUNT(*) FROM scintela.cobro        o WHERE UPPER(TRIM(COALESCE(o.codigo_cli, ''))) = v_cod)
          INTO v_movs;

        -- (1-bis) La única FK real contra `cliente.id_cliente` (migración 0119).
        SELECT COUNT(*) INTO v_proformas
          FROM scintela.proforma_cabecera p
         WHERE p.id_cliente = v_id;

        -- (2) Sobrevive OTRA ficha con el mismo RUC, y esa hermana no puede
        --     ser otra de esta misma lista: si no, dos condenadas se avalan
        --     entre ellas y el cliente desaparece entero.
        v_hermanas := 0;
        IF LENGTH(v_ruc10) = 10 THEN
            SELECT COUNT(*) INTO v_hermanas
              FROM scintela.cliente h
             WHERE h.id_cliente <> v_id
               AND NOT (h.id_cliente = ANY (v_condenadas))
               AND LEFT(REGEXP_REPLACE(COALESCE(h.ruc, ''), '[^0-9]', '', 'g'), 10) = v_ruc10;
        END IF;

        IF v_movs > 0 THEN
            v_problemas := v_problemas || FORMAT(
                'ficha %s (%s): el código juntó %s movimiento(s) desde la revisión del 04/08',
                v_id, v_cod, v_movs);
        ELSIF v_proformas > 0 THEN
            v_problemas := v_problemas || FORMAT(
                'ficha %s (%s): tiene %s proforma(s) apuntando a su id_cliente',
                v_id, v_cod, v_proformas);
        ELSIF v_hermanas = 0 THEN
            v_problemas := v_problemas || FORMAT(
                'ficha %s (%s): ya no queda otra ficha con el RUC %s — borrarla haría desaparecer al cliente',
                v_id, v_cod, COALESCE(NULLIF(v_ruc10, ''), '(sin RUC)'));
        ELSE
            DELETE FROM scintela.cliente WHERE id_cliente = v_id;
            v_borradas := v_borradas + 1;
        END IF;
    END LOOP;

    IF ARRAY_LENGTH(v_problemas, 1) > 0 THEN
        RAISE EXCEPTION 'No pude borrar % de las fichas duplicadas: %',
            ARRAY_LENGTH(v_problemas, 1),
            ARRAY_TO_STRING(v_problemas, ' | ');
    END IF;

    RAISE NOTICE 'Fichas duplicadas borradas en esta corrida: %', v_borradas;
END
$$;


-- El candado. Normalizado (UPPER+TRIM) porque así JOINea todo el sistema:
-- 'lec ' y 'LEC' son el mismo código para la factura, así que también tienen
-- que serlo para la unicidad.
--
-- Excluye los códigos VACÍOS: hay clientes legacy sin código y no queremos
-- que exista un único cliente-sin-código.
--
-- Excluye 7 ids: uno por cada par que sigue abierto. Con el hermano SÍ
-- indexado, el par convive; cualquier código TERCERO que se repita explota
-- igual. Cuando se resuelva cada caso, sacar su id con una migración NUEVA
-- (no editar ésta: ya está aplicada).
--     29142  BLP  Blanca Peñaherrera        (vs 28929 Ramiro Ayo)
--     30547  BRC  Ligia Gualán              (vs 29114 Braulio Cuenca)
--     31469  JQS  Juan Quilumba             (vs 29893 José Yaguachi)
--     29048  LEC  Lola Emperatriz Cisneros  (vs 29008 Luis Ernesto Cañamar)
--     31038  YMO  Yanela Mishell Ortiz      (vs 29995 Yolanda Morocho)
--     32066  JRP  Judith del Rocío Pantoja  (vs 31088 Jorge Rosero — falta el RUC de Judith)
--     31562  MTE  MODITEX                   (vs 29170 MODITEX S.A. — falta saber qué dirección rige)
CREATE UNIQUE INDEX IF NOT EXISTS cliente_codigo_cli_unico
    ON scintela.cliente (UPPER(TRIM(codigo_cli)))
 WHERE COALESCE(TRIM(codigo_cli), '') <> ''
   AND id_cliente NOT IN (29142, 30547, 31469, 29048, 31038, 32066, 31562);

COMMENT ON INDEX scintela.cliente_codigo_cli_unico IS
    'Un código de cliente no se puede repetir (TMT 2026-08-04). Los 7 ids '
    'exceptuados son los pares abiertos de la migración 0155; la lista se '
    'achica, no crece.';
