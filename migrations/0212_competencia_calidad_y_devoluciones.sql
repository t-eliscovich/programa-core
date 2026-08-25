-- 0212 · La competencia cuenta bien: calidad de lo vendido y motivo congelado
--
-- Dos arreglos de la dueña, 24/08/2026, en vísperas de la largada:
--
-- 1 · "dale contemos devoluciones". Las consultas de la competencia miraban
--     sólo los documentos 7 y 251 (las facturas). Las notas de crédito son los
--     documentos 20 y 451 — el módulo de inventario rotativo ya las restaba.
--     Un vendedor facturaba un saldo, sumaba los puntos, el cliente devolvía la
--     tela y los puntos quedaban. Eso se arregla en el SQL de Asinfo, no acá.
--
-- 2 · "solo tiene que ponerse la de segunda en la competencia, la de primera no
--     cuenta". Hay 370 ítems que entraron a la lista SÓLO por sus kilos de
--     segunda (16.124 kg): la tela se vende bien y lo que no sale es la SEG.
--     Pero lo vendido se contaba sin mirar calidad, así que un kilo de PRIMERA
--     de esa misma tela × color puntuaba igual — justo lo que entrar sólo con
--     la SEG buscaba evitar. Para poder separarlo hacen falta dos datos:
--
--       · `parado_venta.calidad` — PRI o SEG de cada kilo vendido. En Asinfo la
--         calidad está en el ATRIBUTO 2 de la línea de factura
--         (`detalle_factura_cliente.id_valor_atributo_2`: 3 = PRI, 4 = SEG).
--         ⚠ NO se puede sacar del lote: `dfc.id_lote` viene en NULL.
--
--       · `parado_cohorte.motivo` — por qué entró el ítem. Se congela igual que
--         los puntos: si mañana la tela entera se para, el ítem no puede
--         cambiar de regla en la mitad de la carrera.

ALTER TABLE scintela.parado_venta
    ADD COLUMN IF NOT EXISTS calidad VARCHAR(3);

-- ⭐ `cuenta` la decide el REFRESH, una sola vez, cruzando la calidad del kilo
-- con el motivo por el que su ítem entró. Las pantallas sólo respetan la
-- bandera. Es a propósito: la regla vive en UN lugar y no en las cuatro
-- consultas que leen esta tabla — con tres WHERE distintos, tarde o temprano
-- una queda sin actualizar y el ranking y el total dejan de coincidir.
ALTER TABLE scintela.parado_venta
    ADD COLUMN IF NOT EXISTS cuenta BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE scintela.parado_cohorte
    ADD COLUMN IF NOT EXISTS motivo VARCHAR(10);

-- ⚠ El motivo NO se rellena acá. La cohorte se marcó el 13/08 y la largada es
-- el 25/08: la foto de hoy ya no es la de aquel día, y un ítem que entró como
-- `parado` y desde entonces vendió un kilo hoy figura como `segunda`. Grabarle
-- ESE motivo lo dejaría con la regla equivocada para toda la carrera, porque el
-- refresh inserta con ON CONFLICT DO NOTHING y nunca más lo corregiría.
-- Lo llena el refresh, una sola vez, la primera vez que corre con este código
-- (ver `actualizar()`): ahí el motivo es el del día en que empieza a contar.

-- Cuántos kilos de SEGUNDA vende la fábrica de cada tela en 12 meses. Es lo
-- que hace falta para ponerle puntaje propio a la segunda: hoy un kilo SEG
-- vale lo mismo que uno PRI de esa tela, sin que nadie haya medido si colocar
-- segunda cuesta más.
ALTER TABLE scintela.parado_punto
    ADD COLUMN IF NOT EXISTS kg_seg_12m NUMERIC(14,2);
