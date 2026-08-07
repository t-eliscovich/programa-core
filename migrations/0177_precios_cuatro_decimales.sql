-- 0177 — precios: 4 decimales en la matriz, y los centavos que quedaron
--        distintos del dBase
--
-- TMT 2026-08-07, la dueña: "tenemos algunos centavos distintos del dbase en
-- precios" → "pone los centavos como dbase".
--
-- Se compararon las 60 celdas de PRECIOS.DBF (última actualización del DBF:
-- 28/04/2026) contra `scintela.precios × 1,15`, que es lo que sale impreso.
-- 56 daban exacto; 4 diferían en UN centavo:
--
--   JASPEADOS/PIQUE   hoja 10,78  ·  dBase 10,77
--   FUERTES/PIQUE     hoja 11,38  ·  dBase 11,39
--   FUERTES/CUELLOS   hoja 14,60  ·  dBase 14,61
--   FUERTES/LYCRA     hoja 13,14  ·  dBase 13,15
--
-- LA CAUSA: la matriz se guarda NETA y la hoja re-multiplica por 1,15, pero
-- la columna era numeric(9,2). El neto exacto de 10,77 es 9,3652 y entraba
-- como 9,37 — al re-multiplicar ya no vuelve el número original. Los otros
-- dos casos (9,90 y 12,70) caen justo en el medio centavo (11,385 y 14,605)
-- y el redondeo los tira para abajo.
--
-- `modules/precios/views.py::guardar` YA escribe con `round(..., 4)` — era la
-- COLUMNA la que no los aguantaba y Postgres los recortaba en silencio. Por
-- eso lo primero es el ALTER: sin él, los UPDATE de abajo vuelven a 2
-- decimales y esta migración no hace nada.
--
-- Los WHERE atan cada celda a su valor mal guardado: es idempotente y NO pisa
-- una corrección hecha a mano por la pantalla (mismo criterio que la 0160).

ALTER TABLE scintela.precios
    ALTER COLUMN jersey   TYPE numeric(9,4),
    ALTER COLUMN pique    TYPE numeric(9,4),
    ALTER COLUMN toper    TYPE numeric(9,4),
    ALTER COLUMN alemania TYPE numeric(9,4),
    ALTER COLUMN rib      TYPE numeric(9,4),
    ALTER COLUMN cuellos  TYPE numeric(9,4),
    ALTER COLUMN lycra    TYPE numeric(9,4),
    ALTER COLUMN falso    TYPE numeric(9,4),
    ALTER COLUMN kiana    TYPE numeric(9,4),
    ALTER COLUMN medical  TYPE numeric(9,4),
    ALTER COLUMN micro    TYPE numeric(9,4),
    ALTER COLUMN james    TYPE numeric(9,4);

-- Los 4 centavos. El valor nuevo es <precio del dBase> / 1,15 a 4 decimales;
-- al volver a multiplicar por 1,15 la hoja imprime EXACTAMENTE el del dBase.
UPDATE scintela.precios SET pique   =  9.3652 WHERE clase = 4 AND pique   =  9.37;  -- JASPEADOS -> 10,77
UPDATE scintela.precios SET pique   =  9.9043 WHERE clase = 5 AND pique   =  9.90;  -- FUERTES   -> 11,39
UPDATE scintela.precios SET cuellos = 12.7043 WHERE clase = 5 AND cuellos = 12.70;  -- FUERTES   -> 14,61
UPDATE scintela.precios SET lycra   = 11.4348 WHERE clase = 5 AND lycra   = 11.43;  -- FUERTES   -> 13,15

-- MEDICAL: las 5 clases tenían 12,02 guardado como si fuera NETO, pero en el
-- dBase 12,02 es el precio CON IVA — nunca se le dividió el 1,15 al importar.
-- No se ve en la hoja porque la tela salió de la lista el 04/08/2026 (está
-- comentada en TELAS), pero si algún día se reactiva saldría a 13,82: un 15%
-- de más. El neto correcto es 12,02 / 1,15 = 10,4522.
UPDATE scintela.precios SET medical = 10.4522 WHERE medical = 12.02;
