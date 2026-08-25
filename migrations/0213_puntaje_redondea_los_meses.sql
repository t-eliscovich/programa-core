-- 0213 · El puntaje compara los meses REDONDEADOS a un decimal
--
-- Dueña 24/08/2026: *"11.98 es igual que 12"*. El corte entre 4 y 10 puntos
-- está en 12 meses de venta parados, y Jersey Forro Spun daba 11,98: sus
-- 2.448 kg —el ítem más grande de la lista— valían 4 en vez de 10 por una
-- diferencia del 0,2%, menos que el error de medición de la bodega.
--
-- El puntaje está CONGELADO desde el 24/08 (`fijado_el`) y el refresh no lo
-- vuelve a escribir mientras haya una fila, así que la corrección va acá y no
-- en el código solo. Se recalcula con los mismos kilos ya guardados: no hace
-- falta volver a preguntarle nada a Asinfo.
--
-- ⚠ Idempotente: se puede correr dos veces y da lo mismo.
-- Medido antes de aplicar: cambia UNA tela (Jersey Forro Spun, de 4 a 10). La
-- bolsa pasa de 227.030 a 241.718 puntos.

UPDATE scintela.parado_punto SET
    nivel = CASE
        WHEN kg_12m < 1                                   THEN 3
        WHEN ROUND(kg_base / (kg_12m / 12.0), 1) < 1      THEN 1
        WHEN ROUND(kg_base / (kg_12m / 12.0), 1) < 12     THEN 2
        ELSE 3 END,
    puntos = CASE
        WHEN kg_12m < 1                                   THEN 10
        WHEN ROUND(kg_base / (kg_12m / 12.0), 1) < 1      THEN 1
        WHEN ROUND(kg_base / (kg_12m / 12.0), 1) < 12     THEN 4
        ELSE 10 END
WHERE kg_base IS NOT NULL AND kg_12m IS NOT NULL;
