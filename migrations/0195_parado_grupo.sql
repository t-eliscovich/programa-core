-- 0195_parado_grupo.sql
-- Grupo de producto en la pantalla de LO PARADO.
-- Dueña 17/08/2026: "tiene que poder filtrarse por grupo de producto y sub
-- grupo".
--
-- En Asinfo el GRUPO es `producto.nombre_categoria_producto` (Fleece, Jersey,
-- Poliester, Pique, Rib, Toper, Lycra, Cuellos, Puños…) y el SUBGRUPO es
-- `nombre_subcategoria_producto`, que es lo que en la pantalla se llama "tela"
-- (Fleece 102, Jersey 3.5, Kiana Forro 1.45…). La pantalla ya mostraba el
-- subgrupo; faltaba el de arriba.
--
-- Sólo hace falta en `parado_foto`: los llamados se agrupan por tela, y la tela
-- ya determina el grupo.

ALTER TABLE scintela.parado_foto
    ADD COLUMN IF NOT EXISTS categoria VARCHAR(80);

CREATE INDEX IF NOT EXISTS idx_parado_foto_categoria
    ON scintela.parado_foto (categoria);
