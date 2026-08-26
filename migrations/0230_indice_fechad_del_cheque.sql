-- 0230 · El índice que le faltaba a la fecha con la que se cuenta la cobranza
--
-- TMT 2026-08-26 (dueña): *"algo más que dure mucho tiempo y podamos bajar"*.
--
-- `cheque.fechad` es la fecha con la que se cuenta TODA la cobranza de la
-- empresa: es la que manda en las comisiones (*"me importa la plata que entró
-- al banco, usar fechad puro"*, 27/05), la del Inicio del vendedor y la de la
-- pantalla de comisiones de la oficina. No tenía índice: cada una de esas
-- pantallas recorría la tabla de cheques ENTERA para juntar un mes.
--
-- Medido el 26/08 contra una base sembrada a escala de producción (3.986
-- clientes, 84.000 facturas, 60.000 cheques), el detalle de cobranza de un
-- vendedor pasó de **549 ms a 5 ms** — con el resultado igual fila por fila.
--
-- ⚠ El índice solo no alcanzaba: el SQL preguntaba por el mes con
-- `EXTRACT(YEAR FROM fechad) = … AND EXTRACT(MONTH FROM fechad) = …`, y una
-- columna envuelta en una función no puede usar ningún índice. Las dos cosas
-- van juntas — ver el comentario grande arriba de `_rango_mes` en
-- modules/comisiones/queries.py.
--
-- ⚠ Y un índice sobre `fechad` SOLO tampoco alcanzaba. Estas pantallas
-- preguntan dos cosas a la vez —"los cheques de ESTE cliente en ESTE mes"—
-- porque cruzan la tabla con los clientes del vendedor. Con un índice por
-- columna suelta, Postgres arma un bitmap de los dos y lo paga UNA VEZ POR
-- CLIENTE: medido, 665 clientes × 0,3 ms = 200 ms. Con el índice de las dos
-- columnas juntas, cada búsqueda es directa.
--
-- Medido acá, los tres casos que se miran todos los días:
--
--                            sin estos índices   con estos índices
--   ventas del mes (viejo)         203,6 ms            8,8 ms
--   cobranza del mes               96,2 ms             8,1 ms
--   /comisiones                    16,0 ms            14,4 ms
--
-- Los tres se quedan: el de `fechad` solo es el que usa /comisiones, que junta
-- el mes de TODOS los vendedores y no filtra por cliente; los otros dos son
-- los del cruce cliente+mes.
--
-- `idx_factura_codigo_cli` (codigo_cli solo) queda cubierto por el nuevo
-- (codigo_cli, fecha) y se podría borrar, pero eso ya no es "agregar un
-- índice": se deja para cuando alguien lo mida.
--
-- Son índices y nada más: no tocan un solo dato.

CREATE INDEX IF NOT EXISTS idx_cheque_fechad
    ON scintela.cheque (fechad);

CREATE INDEX IF NOT EXISTS idx_cheque_cli_fechad
    ON scintela.cheque (codigo_cli, fechad);

CREATE INDEX IF NOT EXISTS idx_factura_cli_fecha
    ON scintela.factura (codigo_cli, fecha);
