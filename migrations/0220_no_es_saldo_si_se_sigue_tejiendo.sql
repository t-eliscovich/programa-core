-- 0220 · La tela que se sigue tejiendo no es un saldo
--
-- Dueña 25/08/2026, cuando le dije que Jersey 3 BLA tenía kilos viejos en la
-- bodega y por eso la regla la dejaba competir: *"pero no es saldo si se seguía
-- produciendo ese color x tela"*.
--
-- Tiene razón, y es más fuerte que las dos reglas anteriores. Que haya kilos
-- viejos de blanco no lo convierte en un saldo si la fábrica tejió 490 kg más
-- el 17/07: eso es un producto VIVO. Nadie necesita una competencia para
-- colocar lo que se está produciendo, y pagar puntos por venderlo es pagar por
-- una venta normal.
--
-- Un ítem sale ahora por tres motivos, y los tres se cuentan por separado
-- porque no significan lo mismo:
--
--   reciente     — la bodega no tenía stock de esa tela × color hace 90 días.
--                  Vuelve sola cuando pase el tiempo.
--   en producción— hay una orden de fabricación de menos de 90 días. Vuelve
--                  cuando la fábrica deje de tejerla.
--   pedida       — hay un pedido de menos de 90 días esperando. Vuelve cuando
--                  el pedido salga.
--
-- ⚠ Las órdenes en `estado_produccion = 0` NO cuentan: no son "programadas",
-- son ABANDONADAS (894 que cuelgan de un padre también en 0 y promedian 660
-- días). Contarlas dejaría media fábrica marcada como viva.

ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS produciendo    INTEGER;
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS produciendo_kg NUMERIC(14,2);

-- ⚠ Tercera y última vez que se rehacen la meta y el puntaje. Se congelan sobre
-- el universo de ítems, y el universo acaba de cambiar otra vez. Sigue siendo
-- el día 0 de la competencia: mañana esto ya no se puede tocar sin moverle el
-- piso a los siete.
DELETE FROM scintela.parado_base;
DELETE FROM scintela.parado_punto;

UPDATE scintela.parado_refresh
   SET actualizado = NULL,
       detalle = 'esperando el refresco que saca la tela que se sigue tejiendo'
 WHERE id = 1;
