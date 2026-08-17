-- 0194_parado_calidad.sql
-- Primera y segunda calidad en la pantalla de LO PARADO.
-- Dueña 17/08/2026: "puedes poner si son de primera o de segunda? eso deberia
-- estar en asinfo". Está: es el ATRIBUTO 2 del LOTE (`valor_atributo` PRI =
-- PRIMERA, SEG = SEGUNDA), no una propiedad del producto — el mismo tela×color
-- puede tener kilos de las dos.
--
-- Por eso son dos columnas y no una: 26 de los ítems parados tienen kilos de
-- primera Y de segunda. Una sola columna "calidad" obligaría a elegir una y
-- perdería la mitad del dato.
--
-- Medido al 17/08/2026: de 37.015 kg parados, 35.499 son de primera y 1.516 de
-- segunda.
--
-- ⚠ El dato sale de `saldo_producto_lote`, que es OTRA tabla que la que usa el
-- resto de la pantalla (`saldo_producto`). Las dos cierran: 333.634 kg contra
-- 333.613 en la bodega 53, 21 kg de diferencia (0,006%). Si algún día se
-- despegan, los kilos por calidad no van a sumar el total de la fila, y eso hay
-- que mirarlo antes de creerle a ninguna de las dos.

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_primera NUMERIC(12,2) NOT NULL DEFAULT 0;

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS kg_segunda NUMERIC(12,2) NOT NULL DEFAULT 0;
