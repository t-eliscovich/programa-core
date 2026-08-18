-- 0203_saldos_limpieza.sql
-- Sacar de la lista las filas que no tienen NADA.
-- Dueña 18/08/2026, mirando filas en 0 kg: "más de 0, si no hay nada para qué
-- están?".
--
-- ⚠ La condición es 0 kg Y 0 vendidos. Una fila con 0 kg que SÍ vendió algo es
-- un ítem que se liquidó entero, y ésa se queda: es la que muestra que la
-- competencia funcionó. Confundir las dos borraría justamente la buena
-- noticia.
--
-- No se pone un mínimo de kilos: la dueña lo descartó. Un ítem de 0,4 kg entra
-- igual, y ahora la pantalla lo muestra con decimal en vez de redondearlo a
-- "0", que era lo que parecía un error.

DELETE FROM scintela.parado_cohorte c
 WHERE EXISTS (
     SELECT 1 FROM scintela.parado_foto f
      WHERE f.subcategoria = c.subcategoria AND f.color = c.color
        AND f.stock_kg <= 0 AND f.kg_vendidos <= 0);

DELETE FROM scintela.parado_foto
 WHERE stock_kg <= 0 AND kg_vendidos <= 0;
