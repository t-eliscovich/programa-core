-- 0222 · Reparar el apagado de más
--
-- El 25/08/2026, poco después de la largada, la tabla de Vendidos apareció
-- VACÍA y los vendedores se quedaron sin puntos. Dueña: "la tabla vendidos está
-- empty".
--
-- QUÉ PASÓ. Las tres banderas —reciente, pedida, en producción— dejaron de
-- mirar "no vendió en 12 meses" para poder contestar también por la tela que YA
-- SE VENDIÓ (si no, apenas se vendía desaparecía de la consulta y se quedaba
-- con los puntos para siempre). Con ese cambio pasaron a contestar por TODA la
-- bodega, incluida tela que nunca fue un saldo parado: cualquier tela que se
-- vende bien y está en producción quedaba marcada `fuera`, y al apagarse se
-- borraban sus ventas de `parado_venta` — que se rehace en cada refresco desde
-- la cohorte encendida.
--
-- Los que se llevaron la peor parte fueron los ítems de SEGUNDA que habían
-- vendido toda su segunda: sin kilos hoy y con la tela en producción, cumplían
-- las dos condiciones del apagado. Justo los que había que premiar.
--
-- EL ARREGLO, en el código: sólo se apaga lo que entró como `parado`. Un ítem
-- de segunda entró por kilos que son un saldo se venda la tela o no, así que
-- ninguna de las tres banderas lo descalifica.
--
-- ACÁ se repara el destrozo. Se apaga TODO el apagado: el refresco recalcula
-- las banderas en cada corrida contra Asinfo, así que vuelve a apagar —ahora
-- con la regla corregida— lo que de verdad no debe estar. Encender de más por
-- un rato es recuperable; dejar a alguien sin sus puntos, no.
UPDATE scintela.parado_cohorte SET fuera = FALSE WHERE fuera;

-- Y la meta y el puntaje otra vez, porque se congelaron hace minutos sobre una
-- cohorte a la que le faltaban ítems. Sigue siendo el día 0.
DELETE FROM scintela.parado_base;
DELETE FROM scintela.parado_punto;

UPDATE scintela.parado_refresh
   SET actualizado = NULL,
       detalle = 'esperando el refresco que repara el apagado de más'
 WHERE id = 1;
