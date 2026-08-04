-- 0160 — precio_plano: sacar el IVA que se descontó de más
--
-- TMT 2026-08-04, la dueña sobre lo que se subió una hora antes:
--   "los precios que subimos recien, les sacamos doblemente IVA, ya estaban
--    sin IVA, corregilos"
--
-- La 0159 leyó su "tiene iva incluido" como que las cifras de la foto venían
-- CON IVA y las guardó divididas por 1,15. Son NETAS. Se ve sin salir de la
-- pantalla: en la matriz KIANA vale 8,88 y en la foto NATY también 8,88 —
-- están en la MISMA escala.
--
-- Las cifras correctas son las de la foto, tal cual:
--   SCUBA 11,25 · SUPLEX 10,21 · BELTIS 11,49 · NATY 8,88
--
-- El WHERE ata cada fila a su valor mal guardado, así que esto es idempotente
-- y NO pisa una corrección hecha a mano por la pantalla.

UPDATE scintela.precio_plano SET precio = 11.25 WHERE tela = 'SCUBA'  AND precio = 9.7826;
UPDATE scintela.precio_plano SET precio = 10.21 WHERE tela = 'SUPLEX' AND precio = 8.8783;
UPDATE scintela.precio_plano SET precio = 11.49 WHERE tela = 'BELTIS' AND precio = 9.9913;
UPDATE scintela.precio_plano SET precio =  8.88 WHERE tela = 'NATY'   AND precio = 7.7217;
