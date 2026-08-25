-- 0223_provisiones_monto_mensual.sql — TMT 2026-08-25
--
-- `scintela.provisiones.importe` vuelve a guardar la cuota MENSUAL.
--
-- Historia: hasta el 28/05/2026 guardaba el mensual; ese día se pasó a
-- guardar la cuota DIARIA ("en vez de mensual hagamos cuota diaria"). El
-- problema que apareció: esa cuota diaria sólo corre de lunes a viernes, así
-- que lo que se devenga en el mes depende de cuántos días hábiles caigan —
-- 21 en agosto 2026, 23 en julio. El gasto del mes no era el mismo mes a mes.
--
-- Pedido de Tamara (25/08/2026), después de ver los números:
--   "hagamos un total mensual de lo que se viene pasando, y luego dividimos
--    por los días" — contando sábados y domingos.
--
-- Conversión: mensual = diaria × 21,75, que son los días hábiles que tiene un
-- mes en promedio (261 al año ÷ 12). Con eso lo que se gasta por AÑO queda
-- igual a lo que se venía gastando; lo que cambia es que ahora todos los
-- meses gastan lo mismo.
--
-- Las 12 de hoy quedan así (diaria → mensual):
--   A,E,C AG,EN,CMB  9.000 → 195.750      13 DEC.TERCERO   1.000 →  21.750
--   RT               8.400 → 182.700      ALQUILER           700 →  15.225
--   SUELDOS          6.000 → 130.500      PROV.INCOBRABLE    400 →   8.700
--   SRI PROVISION    3.300 →  71.775      14 DEC.CUAR+RES    300 →   6.525
--   SS IESS          2.400 →  52.200      INTERESES          300 →   6.525
--   AB PROVISION     1.300 →  28.275      JP JUB.PATRONAL    200 →   4.350
--   Total            33.300 → 724.275
--
-- El reparto diario nuevo arranca el 01/09/2026 (ver reparto_mensual.py):
-- hasta esa fecha el programa divide el mensual por 21,75 y sigue corriendo
-- sólo de lunes a viernes, o sea que agosto no se mueve ni un peso.
--
-- Idempotente: deja la marca en scintela.sistema_meta y no vuelve a
-- multiplicar si se corre dos veces.

DO $$
DECLARE
    ya TEXT;
    n INTEGER;
BEGIN
    SELECT valor INTO ya
      FROM scintela.sistema_meta
     WHERE clave = 'provisiones_importe_es_mensual';

    IF ya IS NOT NULL THEN
        RAISE NOTICE 'Las provisiones ya están en monto mensual (%). No toco nada.', ya;
        RETURN;
    END IF;

    UPDATE scintela.provisiones
       SET importe = ROUND(importe * 21.75, 2),
           fecha_actualiza = CURRENT_DATE,
           usuario_actualiza = 'mig-0223'
     WHERE COALESCE(importe, 0) > 0;
    GET DIAGNOSTICS n = ROW_COUNT;

    INSERT INTO scintela.sistema_meta (clave, valor)
    VALUES ('provisiones_importe_es_mensual', CURRENT_DATE::text);

    RAISE NOTICE 'Provisiones pasadas a monto mensual: % filas (× 21,75).', n;
END $$;
