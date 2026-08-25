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
-- Ahora hace falta además que sus rollos estén en la bodega desde hace más de 6
-- meses (`asinfo_parado.MESES_QUIETO`, elegido por la dueña entre 3, 6 y 12).
-- La de SEGUNDA entra igual, se haya hecho cuando se haya hecho: eso no cambia
-- ("sí, la segunda siempre entra").

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
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS nuevas    INTEGER;
ALTER TABLE scintela.parado_refresh
    ADD COLUMN IF NOT EXISTS nuevas_kg NUMERIC(14,2);

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
       detalle = 'esperando el refresco que saca la tela reciente'
 WHERE id = 1;
