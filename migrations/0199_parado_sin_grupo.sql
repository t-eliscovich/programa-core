-- 0199_parado_sin_grupo.sql
-- Sacar de la cohorte las filas fantasma: sin stock Y sin grupo.
--
-- La 0196 borró la tela cruda por NOMBRE y quedó una fila que no matcheaba.
-- Se ve en la pantalla como un renglón "(sin grupo) · 0 kg · 0,0%" que no
-- significa nada.
--
-- ⚠ La condición lleva LAS DOS cosas. Una fila con 0 kg y CON grupo es un ítem
-- que se vendió entero, y ésa tiene que quedarse: es justamente lo que la
-- dueña pidió que no desaparezca ("si empezamos a venderlas, que no se nos
-- vayan de la lista"). Sin grupo pero con kilos tampoco se toca: sería un dato
-- a completar, no basura.

DELETE FROM scintela.parado_cohorte c
 WHERE EXISTS (
     SELECT 1 FROM scintela.parado_foto f
      WHERE f.subcategoria = c.subcategoria AND f.color = c.color
        AND f.categoria IS NULL AND f.stock_kg = 0);

DELETE FROM scintela.parado_foto
 WHERE categoria IS NULL AND stock_kg = 0;
