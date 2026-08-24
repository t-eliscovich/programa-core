-- Los PUNTOS por tela de la competencia de saldos.
--
-- Hasta ahora todo kilo valía lo mismo y el puesto salía de los kilos. Medido
-- el 24/08/2026: los 8 grupos venden mucho más por mes de lo que tienen
-- parado (entre 0,1 y 0,9 meses), así que a nivel grupo no hay ninguna señal
-- de dificultad. A nivel TELA sí: va de 0,0 meses (Fleece 102, que vende 54 t
-- por mes y tiene 1.163 kg parados) a telas que no vendieron un kilo en todo
-- el año. Por eso el puntaje vive acá, por tela, y no por grupo.
--
-- ⭐ Se escribe UNA vez, el día de la largada, y no se toca más. Si el nivel
-- se recalculara solo, un vendedor que saca 500 kg de una tela le baja los
-- meses parados a esa tela, la tela cae de nivel y él mismo se recorta los
-- puntos a mitad de camino.
CREATE TABLE IF NOT EXISTS scintela.parado_punto (
    subcategoria  TEXT PRIMARY KEY,
    categoria     TEXT,
    kg_base       NUMERIC(12,2) NOT NULL DEFAULT 0,
    kg_12m        NUMERIC(12,2) NOT NULL DEFAULT 0,
    meses         NUMERIC(12,2),
    nivel         SMALLINT      NOT NULL,
    puntos        SMALLINT      NOT NULL,
    fijado_el     DATE          NOT NULL
);
