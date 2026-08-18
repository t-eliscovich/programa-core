-- 0198_competencia_config.sql
-- La meta total y la fecha de largada de la competencia.
-- Dueña 17/08/2026: la meta es "todo" (el 100% de lo parado) y cuenta "desde
-- hoy, 17/08".
--
-- ⭐ Por qué cambia la fórmula: la primera versión le ponía a cada grupo, como
-- meta, su propio peso en el parado (Jersey 31,5% → sacarle el 31,5%). Eso da
-- un total de 21,7% que nadie decidió, y al grupo más chico una meta de 4 kg.
-- Ahora es al revés y es lo que ella quiso decir: LA DUEÑA PONE EL TOTAL y ese
-- total se reparte entre los grupos según lo que pesa cada uno — o sea, todos
-- los grupos con la misma exigencia. Con el total en 100%, cada grupo tiene que
-- despejar sus propios kilos.
--
-- ⚠ La LARGADA no es la fecha en que se marcó la cohorte (13/08). Son cosas
-- distintas: la cohorte define QUÉ está parado, la largada desde CUÁNDO cuentan
-- los kilos para la competencia. Sin separarlas, los cuatro días previos le
-- habrían regalado kilos a quien justo vendió algo.

CREATE TABLE IF NOT EXISTS scintela.parado_config (
    clave        VARCHAR(40) PRIMARY KEY,
    valor        VARCHAR(40) NOT NULL,
    actualizado  TIMESTAMP   NOT NULL DEFAULT NOW(),
    quien        VARCHAR(60)
);

INSERT INTO scintela.parado_config (clave, valor) VALUES
    ('meta_total_pct', '100'),
    ('largada', '2026-08-17')
ON CONFLICT (clave) DO NOTHING;
