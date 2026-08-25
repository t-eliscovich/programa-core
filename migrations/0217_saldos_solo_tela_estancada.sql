-- 0217 · La competencia, sólo con tela estancada
--
-- Dueña 25/08/2026, el día de la largada: "ver que la competencia tenga solo
-- tela estancada, no tela que se hizo recientemente y no se movió".
--
-- El filtro miraba dos cosas —más de 20 kg en bodega y ni un kilo vendido en 12
-- meses— y ninguna de las dos habla del TIEMPO que la tela lleva ahí. Una tela
-- que salió de producción el mes pasado cumple las dos: no vendió nada porque
-- todavía no tuvo la chance. Entraba a la lista, sumaba kilos a la meta y le
-- daba puntos a quien la vendiera, igual que una clavada desde 2019.
--
-- Medido sobre los 344 ítems parados del 25/08: 47 se habían hecho en los
-- últimos 6 meses (4.406 kg). Nueve de ellos eran las 6 Pique Nido, las 2
-- Jersey 115 y la Fleece 2.2 BHU: tejidas entre el 12 y el 24 de agosto contra
-- ocho pedidos de VEGA LOGRO y uno de TEXTILES EL GRECO. Tela con dueño, no
-- saldo.
--
-- Con eso la dueña cerró la regla. Un ítem que no vendió un kilo en 12 meses
-- entra a la lista sólo si:
--
--   · la bodega YA TENÍA stock de esa tela × color hace 90 días
--     (`asinfo_parado.DIAS_QUIETO`; probamos 6 meses y eligió 90 días: "así no
--     sacamos tanto"), y
--   · no tiene un pedido de menos de 90 días esperando
--     (`DIAS_PEDIDO`): "si la tela se produjo por un pedido, tiene que salir de
--     la competencia. si es hace más de 90 días asumo que quedó estancada".
--
-- La de SEGUNDA entra igual, se haya hecho cuando se haya hecho y esté pedida o
-- no: el pedido es de primera, y esos kilos siguen siendo un saldo ("sí, la
-- segunda siempre entra").
--
-- ⚠ La antigüedad se mide por el SALDO DEL PRODUCTO, no por la fecha de los
-- rollos. El 11 y el 25/04/2026 un re-loteo de bodega le creó rollos nuevos a
-- tela vieja sin una sola orden de fabricación detrás: midiendo por rollo, Rib
-- Spun AMF —última venta 17/11/2022— figuraba como producción fresca. Eran 12
-- ítems y 314 kg de la tela más clavada que hay.

-- ── 1 · La que nunca debió entrar se APAGA, no se borra ─────────────────────
-- La cohorte es deliberadamente inmutable ("si empezamos a venderlas, que no se
-- nos vayan de la lista"), y hasta hoy sacar algo era una migración a mano
-- (0196, tela cruda). Esto no se puede hacer a mano: si una tela es reciente lo
-- dice Asinfo, no una lista de nombres, y deja de serlo sola con el tiempo. Por
-- eso es una marca que el refresco prende y apaga, y la fila queda con su
-- `fecha_marcado` para el día que la tela cumpla los meses y vuelva.
ALTER TABLE scintela.parado_cohorte
    ADD COLUMN IF NOT EXISTS fuera BOOLEAN NOT NULL DEFAULT FALSE;

-- ── 2 · Cuántas quedaron afuera, para poder verlo en la pantalla ────────────
-- Las dos razones van separadas: en la pantalla no significan lo mismo. La
-- reciente se arregla sola con el tiempo; la pedida, cuando salga el pedido.
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS nuevas     INTEGER;
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS nuevas_kg  NUMERIC(14,2);
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS pedidas    INTEGER;
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS pedidas_kg NUMERIC(14,2);

-- ── 3 · La meta y los puntos se vuelven a congelar ─────────────────────────
-- ⚠ Esto es lo ÚNICO de esta migración que no se puede repetir a la ligera. La
-- meta en kilos (`parado_base`) y el puntaje de cada tela (`parado_punto`) se
-- congelan una sola vez y no se tocan más, justamente para que el tablero no se
-- mueva abajo de los vendedores. Se vuelven a congelar acá porque los dos se
-- fijaron sobre un universo que incluía tela recién hecha, y porque la largada
-- es HOY: se corrige el día 0, no en la mitad de la carrera. El primer refresco
-- después de esta migración los escribe de nuevo, ya sin la tela nueva adentro.
DELETE FROM scintela.parado_base;
DELETE FROM scintela.parado_punto;

-- Y que ese refresco sea el próximo, no el de dentro de tres horas: el hilo de
-- fondo mira esta fecha para decidir si toca (`analisis.auto_refresco`).
UPDATE scintela.parado_refresh
   SET actualizado = NULL,
       detalle = 'esperando el refresco que saca la tela reciente y la pedida'
 WHERE id = 1;
