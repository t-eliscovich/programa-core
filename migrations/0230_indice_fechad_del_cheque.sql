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
-- ⚠ MEDIDO ACÁ y no en producción: una base local sembrada con la FORMA que
-- tiene la de la fábrica según los números verificados que hay en el repo —
-- 3.986 clientes, 35.526 facturas (facturas/views.py:3052, 14/08/2026) y 4.526
-- cheques (~3.200 vinieron del dBase + ~476 por mes), con 476 cheques en el mes
-- que se consulta.
--
-- ⚠ Y un índice sobre `fechad` SOLO no alcanza. Estas pantallas preguntan dos
-- cosas a la vez —"los cheques de ESTE cliente en ESTE mes"— porque cruzan la
-- tabla con los clientes del vendedor. Con un índice por columna suelta,
-- Postgres arma un bitmap de los dos y lo paga UNA VEZ POR CLIENTE. Por eso van
-- los tres, y por eso van JUNTO con el cambio del SQL: solos no hacen nada,
-- porque el `EXTRACT` los ignora.
--
--                                    hoy   sólo estos índices   índices + rango
--   cobranza del mes (un vendedor) 343 ms         348 ms              6,2 ms
--   ventas del mes (un vendedor)    13 ms          13 ms              6,8 ms
--   comisión mes a mes del año     101 ms         103 ms              9,0 ms
--   /comisiones (los 6 vendedores)  26 ms          25 ms             11,4 ms
--
-- Los tres se quedan: el de `fechad` solo es el que usa /comisiones, que junta
-- el mes de TODOS los vendedores y no filtra por cliente; los otros dos son los
-- del cruce cliente+mes.
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
