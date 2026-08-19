-- 0204_competencia_base.sql
-- La META se CONGELA el día de la largada.
--
-- Dueña 18/08/2026, después de ver el número moverse solo: el mismo día, sin
-- una sola venta, «había al arrancar» pasó de 52.407 a 51.654 kg — 753 kg de
-- ajustes de bodega. La meta se reconstruía en cada lectura como «lo que hay
-- hoy + lo vendido», así que un ajuste de stock le subía o le bajaba el
-- porcentaje a los siete sin que ninguno hubiera vendido nada. En una
-- competencia que dura cuatro meses eso es fatal: el puntaje tiene que
-- moverse SÓLO cuando alguien vende.
--
-- Acá se guardan los kilos por grupo del día de la largada. Se escriben UNA
-- vez, en el primer refresco del 25/08 o después, y ya no se tocan.

CREATE TABLE IF NOT EXISTS scintela.parado_base (
    categoria  VARCHAR(60)    PRIMARY KEY,
    kg         NUMERIC(14, 2) NOT NULL,
    fijada_el  DATE           NOT NULL,
    creado_en  TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
