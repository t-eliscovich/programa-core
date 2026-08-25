-- 0218 · La meta y el puntaje, otra vez, con el tope por ítem
--
-- Dueña 25/08/2026, mirando el tablero después del primer deploy: "está mal que
-- sigue contando una tela que había 0 en saldo". Intela tenía 595 puntos y 554
-- eran UNA venta de Jersey 3 BLA — tela tejida el 17/07 (orden OFT-000039674.1,
-- 490 kg) que se vendió el día de la largada.
--
-- La regla de antigüedad de la 0217 mira el ÍTEM: como esa tela × color tenía
-- unos kilos viejos de blanco en la bodega al corte, entró entera y los 490 kg
-- de julio puntuaron como si hubieran estado clavados. El arreglo no es sacar
-- la tela —los kilos viejos SÍ estaban parados— sino ponerle un TOPE: un ítem
-- no puede puntuar más kilos de los que ya tenía en la bodega al corte. Lo que
-- se venda por encima se vende igual, pero no destraba nada.
--
-- Eso vive en el refresco (`queries.actualizar`), que rehace `parado_venta` en
-- cada corrida. Lo que NO se rehace solo es lo congelado:
--
--   · `parado_base`  — la meta en kilos, que se fijó hace unos minutos como
--                      "stock + lo vendido", con esos kilos de más adentro.
--   · `parado_punto` — el puntaje por tela, calculado sobre esos mismos kilos.
--
-- Se vuelven a congelar acá por última vez. Sigue siendo el día 0 de la
-- competencia: es el único momento en que esto se puede hacer sin moverle el
-- piso a nadie.
DELETE FROM scintela.parado_base;
DELETE FROM scintela.parado_punto;

-- Y que el próximo refresco sea el de dentro de dos minutos, no el de dentro de
-- tres horas (`analisis.auto_refresco` mira esta fecha).
UPDATE scintela.parado_refresh
   SET actualizado = NULL,
       detalle = 'esperando el refresco que aplica el tope por ítem'
 WHERE id = 1;
