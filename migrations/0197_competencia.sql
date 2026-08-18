-- 0197_competencia.sql
-- La COMPETENCIA para liquidar lo parado.
-- Dueña 17/08/2026: "vamos a hacer una competencia, pone una tab mas que se
-- llame competencia. aca tienen acceso todos, vendedores sobre todo incluidos".
--
-- Reglas que eligió, y que explican por qué las tablas son éstas:
--
-- ⭐ La meta es POR GRUPO, no por tela ni global ("1, pero de cada grupo").
-- ⭐ La meta de un grupo sale de su PESO en el stock parado ("meta segun
--    cantidad de ese grupo % en el total del stock"): Jersey es el 31,5% de lo
--    parado, así que su meta es sacar el 31,5% de sus propios kilos. Se calcula
--    sola; `parado_meta` existe sólo para PISARLA a mano cuando haga falta.
-- ⭐ La meta del grupo se reparte en PARTES IGUALES entre los que compiten.
--    Dueña 17/08/2026: "nono igual para todos. todos deberian tener lugares
--    fuertes y debiles asumo". Y tiene razón con el dato en la mano: Patricio
--    es el 26,6% de Fleece y el 2,6% de Jersey; Felipe el 21% de Poliester y
--    el 2,5% de Fleece. Repartir por el histórico habría premiado quedarse en
--    lo de siempre, que es lo contrario de lo que busca la competencia.
--    `parado_share` se sigue calculando, pero como INFORMACIÓN en pantalla
--    (quién domina cada grupo), no como meta.
-- ⭐ Intela (el mostrador) COMPITE como uno más — "hay una vendedora dedicada".
--    Es el 51,3% de las ventas de estas telas, así que va a estar arriba: es
--    una decisión tomada con el dato a la vista, no un descuido.

-- Lo que se vendió de la cohorte, con vendedor. Se rehace en cada refresh.
CREATE TABLE IF NOT EXISTS scintela.parado_venta (
    subcategoria  VARCHAR(120)  NOT NULL,
    color         VARCHAR(10)   NOT NULL,
    vend_pc       VARCHAR(3),
    vendedor      VARCHAR(80)   NOT NULL,
    fecha         DATE          NOT NULL,
    kg            NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_parado_venta_tela
    ON scintela.parado_venta (subcategoria, color);
CREATE INDEX IF NOT EXISTS idx_parado_venta_vend
    ON scintela.parado_venta (vendedor);

-- El % histórico que cada vendedor vende de cada GRUPO. Se rehace en cada
-- refresh. Es lo que reparte la meta.
CREATE TABLE IF NOT EXISTS scintela.parado_share (
    categoria     VARCHAR(80)   NOT NULL,
    vend_pc       VARCHAR(3),
    vendedor      VARCHAR(80)   NOT NULL,
    kg            NUMERIC(14,2) NOT NULL DEFAULT 0,
    pct           NUMERIC(6,2)  NOT NULL DEFAULT 0,
    PRIMARY KEY (categoria, vendedor)
);

-- Sólo para PISAR la meta automática de un grupo. Vacía = todo automático.
CREATE TABLE IF NOT EXISTS scintela.parado_meta (
    categoria     VARCHAR(80)  PRIMARY KEY,
    pct           NUMERIC(6,2) NOT NULL,
    actualizado   TIMESTAMP    NOT NULL DEFAULT NOW(),
    quien         VARCHAR(60)
);
