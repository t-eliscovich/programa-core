-- 0221_amortizacion_dias_reales.sql — TMT 2026-08-25
--
-- Pedido de Tamara (25/08/2026): "que se calcule y se incremente todos los
-- días. Mismo monto mensual, pero si es 100 entonces cada día de un mes de
-- 31 se sube 100/31, si es 30 entonces 100/30, contando sábados y domingos".
--
-- La regla vieja (MENU.PRG L275) repartía la cuota SIEMPRE entre 30:
--     coef = min(día, 30) / 30
-- y dejaba dos meses torcidos: en un mes de 31 días el 31 no movía nada
-- (el 30 ya había llegado al tope), y febrero cerraba en 28/30 = 93,3% con
-- el resto saltando el 1 de marzo.
--
-- Regla nueva: coef = día / días del mes. El total del mes NO cambia; el
-- día del cierre da exactamente 1 en todos los meses.
--
-- CORTE 2026-09-01: antes de esa fecha se sigue dividiendo por 30, así el
-- cambio entra sin escalón (septiembre tiene 30 días → las dos fórmulas dan
-- lo mismo) y ningún mes ya vivido se mueve hacia atrás.
--
-- La misma cuenta del lado de Python vive en `amortizacion.py`.

CREATE OR REPLACE FUNCTION scintela.coef_amortizacion(d date) RETURNS numeric
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN d >= DATE '2026-09-01' THEN
            EXTRACT(DAY FROM d)::numeric
            / EXTRACT(DAY FROM (date_trunc('month', d) + INTERVAL '1 month' - INTERVAL '1 day'))::numeric
        ELSE
            LEAST(EXTRACT(DAY FROM d)::numeric, 30) / 30.0
    END;
$$;

COMMENT ON FUNCTION scintela.coef_amortizacion(date) IS
    'Qué parte de la cuota mensual de un activo ya corrió al día d (0 a 1). '
    'Desde el 01/09/2026 se reparte entre los días reales del mes; antes, '
    'entre 30 (regla vieja de MENU.PRG). Espejo de amortizacion.py.';

-- La proc mensual usaba su propio min(día,30)/30 inline. Que llame a la
-- función, así la regla vive en un solo lugar.
-- Igual que la 0024: si la función ya existe y es de otro rol, CREATE OR
-- REPLACE falla con 'must be owner of function'. Intentamos tomarla; si no
-- se puede, dejamos el aviso y seguimos.
DO $do$
BEGIN
    BEGIN
        EXECUTE 'ALTER FUNCTION scintela.actualizar_amortizacion() OWNER TO '
                || quote_ident(current_user);
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'Sin privilegio para tomar scintela.actualizar_amortizacion. Si falla abajo, correr como superuser: ALTER FUNCTION scintela.actualizar_amortizacion() OWNER TO %', current_user;
        WHEN undefined_function THEN
            NULL;
    END;
END $do$;

CREATE OR REPLACE FUNCTION scintela.actualizar_amortizacion() RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    coef NUMERIC;
    yyyymm INTEGER;
    hoy DATE;
BEGIN
    hoy := (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date;   -- hoy en Ecuador

    -- Año*100 + mes — comparable entre años distintos.
    yyyymm := EXTRACT(YEAR FROM hoy)::int * 100
            + EXTRACT(MONTH FROM hoy)::int;

    -- 1) Sumar la cuota del mes que cerró al acumulado, sólo si el mes
    --    ACTUAL (año+mes) todavía no fue procesado para este activo.
    UPDATE scintela.activos
       SET amortizac = COALESCE(amortizac, 0) + COALESCE(cuota, 0)
     WHERE COALESCE(ult_mes_amortizado, 0) IS DISTINCT FROM yyyymm
       AND COALESCE(cuota, 0) > 0
       AND COALESCE(inicial, 0) - COALESCE(amortizac, 0) > 0.01;

    -- 2) Qué parte de la cuota del mes en curso ya corrió al día de hoy.
    coef := scintela.coef_amortizacion(hoy);

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
    'por (año*100+mes) en ult_mes_amortizado. El prorrateo del mes en '
    'curso sale de scintela.coef_amortizacion(hoy).';

-- Entre cuántos días se parte la cuota del mes — para la columna "Dep/día"
-- de la pantalla de activos.
CREATE OR REPLACE FUNCTION scintela.divisor_amortizacion(d date) RETURNS numeric
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN d >= DATE '2026-09-01' THEN
            EXTRACT(DAY FROM (date_trunc('month', d) + INTERVAL '1 month' - INTERVAL '1 day'))::numeric
        ELSE 30.0
    END;
$$;

COMMENT ON FUNCTION scintela.divisor_amortizacion(date) IS
    'Entre cuántos días se reparte la cuota mensual de un activo en el mes '
    'de d. Desde el 01/09/2026, los días reales del mes; antes, 30.';
