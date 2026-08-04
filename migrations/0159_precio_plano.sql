-- 0159 — scintela.precio_plano (telas de PRECIO ÚNICO, no van en la matriz)
--
-- TMT 2026-08-04. La dueña mandó una foto de la tablita que usa aparte de la
-- matriz de PRECIOS.DBF, con el texto: "a proforma y precios agregar esto,
-- tiene iva incluido":
--
--     JERSEY 3,5  |  PRECIO DE JERSEY
--     JERSEY 3    |  PRECIO DE JERSEY
--     SCUBA       |  11,25 TODOS LOS COLORES
--     SUPLEX      |  10,21 TODOS LOS COLORES
--     BELTIS      |  11,49 TODOS LOS COLORES
--     NATY        |   8,88 TODOS LOS COLORES
--
-- Son telas que NO tienen precio por clase de color: uno solo para todas. Por
-- eso no entran como columna de scintela.precios (sería la misma cifra 5
-- veces) y viven en su propia tabla, en el mismo orden en que ella las
-- escribió (`orden`).
--
-- DOS formas de fijar el precio, y sólo una por fila:
--   * `precio`  — la cifra, SIN IVA (ver abajo).
--   * `ref_col` — "cobra lo mismo que <columna de la matriz>". Es lo que hace
--     JERSEY 3,5 / JERSEY 3 (= 'jersey'): si mañana sube el jersey, suben
--     solos. Guardar una copia del número los desincronizaría en silencio.
--
-- ⚠ EL PRECIO SE GUARDA SIN IVA. Las cifras de la foto vienen CON IVA 15%
-- incluido ("tiene iva incluido"), pero toda la lista de precios de Programa
-- Core es neta y el IVA se agrega al imprimir (checkbox "Con IVA 15%" en
-- /precios/imprimir). Si esto se guardara con IVA, la hoja con IVA lo
-- cobraría dos veces. Por eso el seed divide por 1,15 y se guardan 4
-- decimales, para que al re-multiplicar vuelva EXACTO el número de la foto:
--     11,25 / 1,15 = 9,7826  ->  x1,15 = 11,2500  OK
--     10,21 / 1,15 = 8,8783  ->  x1,15 = 10,2100  OK
--     11,49 / 1,15 = 9,9913  ->  x1,15 = 11,4900  OK
--      8,88 / 1,15 = 7,7217  ->  x1,15 =  8,8800  OK
--
-- ⚠ El deploy NO corre migraciones: la tabla también se bootstrapea en
-- caliente desde `modules/precios/queries.py::bootstrap_precio_plano`. Este
-- archivo existe para que el esquema quede documentado y para entornos nuevos.

CREATE TABLE IF NOT EXISTS scintela.precio_plano (
    id            SERIAL PRIMARY KEY,
    orden         INTEGER      NOT NULL DEFAULT 0,
    tela          VARCHAR(40)  NOT NULL UNIQUE,
    precio        NUMERIC(12, 4),
    ref_col       VARCHAR(20),
    nota          VARCHAR(60)  NOT NULL DEFAULT 'Todos los colores',
    actualizado   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    usuario_edita VARCHAR(50)
);

COMMENT ON TABLE scintela.precio_plano IS
    'Telas de precio unico (el mismo para todas las clases de color). '
    'Complementa la matriz scintela.precios. `precio` va SIN IVA.';
COMMENT ON COLUMN scintela.precio_plano.precio IS
    'Precio USD/kg SIN IVA. NULL si la fila usa ref_col.';
COMMENT ON COLUMN scintela.precio_plano.ref_col IS
    'Columna de scintela.precios cuyo precio se cobra (ej. jersey). '
    'Excluyente con `precio`.';

INSERT INTO scintela.precio_plano (orden, tela, precio, ref_col, nota) VALUES
    (1, 'JERSEY 3,5', NULL,   'jersey', 'Precio de JERSEY'),
    (2, 'JERSEY 3',   NULL,   'jersey', 'Precio de JERSEY'),
    (3, 'SCUBA',      9.7826,  NULL,    'Todos los colores'),
    (4, 'SUPLEX',     8.8783,  NULL,    'Todos los colores'),
    (5, 'BELTIS',     9.9913,  NULL,    'Todos los colores'),
    (6, 'NATY',       7.7217,  NULL,    'Todos los colores')
ON CONFLICT (tela) DO NOTHING;
