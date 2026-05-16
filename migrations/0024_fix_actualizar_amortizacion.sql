-- 0024_fix_actualizar_amortizacion.sql — TMT 2026-05-12
--
-- Bug del PRG legacy que sobrevivió en la function de Postgres:
-- `ult_mes_amortizado` se comparaba con `EXTRACT(MONTH FROM current_date)`,
-- que devuelve sólo el número de mes 1-12. Si la function NO corre durante
-- 12 meses exactos, al cumplirse el "mismo mes" del año siguiente el filtro
-- dice "ya lo procesé" y NO suma la cuota. Resultado: año perdido en la
-- amortización acumulada.
--
-- Fix: comparar `año*100 + mes` (que ya es la convención del código Python
-- alrededor — ver activos/queries.py).
--
-- Bonus: la versión vieja recalculaba `amortimes` y `valor` de TODAS las
-- filas sin filtrar por mes. Lo dejamos igual porque el último UPDATE
-- (cuando valor <= 0) limpia los casos de overshoot. Pero documentamos
-- la lógica para que el próximo lector entienda.
--
-- TMT 2026-05-13: intento auto-tomar ownership porque algunos entornos
-- (incluido el dev local de Tamara) tienen la function creada por otro
-- rol y CREATE OR REPLACE falla con `must be owner of function`. Si no
-- tengo privilegios para cambiar el dueño, dejo el aviso claro y sigo —
-- si la function ya no es mía, CREATE OR REPLACE seguirá fallando y va
-- a haber que correr manualmente:
--     ALTER FUNCTION scintela.actualizar_amortizacion() OWNER TO <user>;
DO $$
BEGIN
    BEGIN
        EXECUTE 'ALTER FUNCTION scintela.actualizar_amortizacion() OWNER TO ' || quote_ident(current_user);
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'No tengo privilegio para tomar ownership de actualizar_amortizacion. Si CREATE OR REPLACE falla acá abajo, ejecutá como superuser: ALTER FUNCTION scintela.actualizar_amortizacion() OWNER TO %', current_user;
        WHEN undefined_function THEN
            -- La function no existe todavía — CREATE OR REPLACE la crea desde cero.
            NULL;
    END;
END $$;

CREATE OR REPLACE FUNCTION scintela.actualizar_amortizacion() RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    coef NUMERIC;
    yyyymm INTEGER;
BEGIN
    -- Año*100 + mes — comparable entre años distintos.
    yyyymm := EXTRACT(YEAR FROM current_date)::int * 100
            + EXTRACT(MONTH FROM current_date)::int;

    -- 1) Sumar la cuota mensual al acumulado, sólo si el mes ACTUAL
    --    (año+mes) todavía no fue procesado para este activo.
    UPDATE scintela.activos
       SET amortizac = COALESCE(amortizac, 0) + COALESCE(cuota, 0)
     WHERE COALESCE(ult_mes_amortizado, 0) IS DISTINCT FROM yyyymm
       AND COALESCE(cuota, 0) > 0
       AND COALESCE(inicial, 0) - COALESCE(amortizac, 0) > 0.01;

    -- 2) Calcular el coeficiente del prorrateo del mes en curso.
    --    Día 30 o más → mes completo. Si no, día/30.
    IF EXTRACT(DAY FROM current_date) > 30 THEN
        coef := 1;
    ELSE
        coef := EXTRACT(DAY FROM current_date)::numeric / 30;
    END IF;

    -- 3) Aplicar la cuota prorrateada como "amortización del mes en curso"
    --    y recalcular el valor en libros visible.
    UPDATE scintela.activos
       SET amortimes = coef * COALESCE(cuota, 0),
           valor     = COALESCE(inicial, 0) - COALESCE(amortizac, 0)
                       - (coef * COALESCE(cuota, 0));

    -- 4) Activos completamente amortizados: blanquear cuota/amortimes/valor
    --    y mantener amortizac = inicial. Idempotente — no se vuelve a tocar.
    UPDATE scintela.activos
       SET amortizac = inicial,
           amortimes = 0,
           valor     = 0,
           cuota     = 0
     WHERE COALESCE(valor, 0) <= 0
       AND COALESCE(inicial, 0) > 0;

    -- 5) Marcar el mes (año+mes) como procesado en cada fila que tocamos.
    UPDATE scintela.activos
       SET ult_mes_amortizado = yyyymm
     WHERE COALESCE(ult_mes_amortizado, 0) IS DISTINCT FROM yyyymm;
END;
$$;

COMMENT ON FUNCTION scintela.actualizar_amortizacion() IS
    'Aplicar amortización mensual a scintela.activos. Idempotente '
    'por (año*100+mes) en ult_mes_amortizado. Reemplaza versión vieja '
    'que comparaba solo MONTH y perdía un año exacto sin correr.';
