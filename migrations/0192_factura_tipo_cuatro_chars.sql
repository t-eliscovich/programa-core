-- 0192 — `factura.tipo` pasa de varchar(2) a varchar(4).
--
-- TMT 2026-08-14 (dueña). La columna de DOS caracteres es la causa raíz de que
-- el tipo de documento esté mal: el importador de Asinfo guarda
-- `tipo_asinfo[:2]`, y ahí `NC_FINANCIERA` y `NCNT` se convierten las dos en
-- "NC". Medido en producción ese día: 253 filas "NC", de las cuales 235 son
-- financieras (kg = 0) y 18 son NCNT (kg < 0), mezcladas sin forma de
-- distinguirlas más que por el signo.
--
-- Cuatro caracteres es lo que mide el más largo del vocabulario único que
-- eligió la dueña ("llamalas NC y NCNT y devolución, mantengamos lo que trae
-- el Asinfo") — los mismos códigos que la pantalla ya mostraba en el combo.
-- El límite queda puesto en la base para que nadie vuelva a guardar un nombre
-- recortado en silencio:
--
--     F    Factura            D     Devolución        N    NTEN
--     NC   NC financiera      NCNT  NCNT
--
-- Esta migración NO cambia ningún valor: sólo ensancha. El remapeo de los dos
-- idiomas viejos se hace por pantalla, con dry-run previo — pedido explícito
-- de la dueña ("antes de hacer lo que hagas quiero que corras un dry run para
-- no dañar nada").
ALTER TABLE scintela.factura
    ALTER COLUMN tipo TYPE varchar(4);

-- El filtro de /facturas pasa a ir por SQL (antes recortaba en Python después
-- de paginar, y por eso en la pestaña Estado daba cero).
CREATE INDEX IF NOT EXISTS idx_factura_tipo
    ON scintela.factura (tipo);
