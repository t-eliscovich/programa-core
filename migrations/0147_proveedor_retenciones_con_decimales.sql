-- 0147 — Las retenciones del proveedor admiten DECIMALES.
--
-- TMT 2026-07-31. Al estrenar la edición inline de /proveedores se tipeó
-- `1,75` en Ret. base de PUGI GROUP y la pantalla devolvió **2,00**: las
-- columnas eran INTEGER y Postgres redondeaba sin decir nada.
--
-- En Ecuador la retención en la fuente sobre la base tiene medios puntos
-- reales (1 · 1,75 · 2 · 2,75 · 8 · 10 %), así que INTEGER no alcanza para el
-- dato que la pantalla dice guardar. El importador de la ficha del dBase
-- (`proveedores_import_view._num`) ya las leía como float — o sea que el
-- redondeo también se comía los decimales de cada importación, en silencio,
-- desde siempre.
--
-- Ni retbase ni retiva entran en ningún cálculo del programa: son el DATO de
-- referencia de quién emite la retención. Ensanchar la columna no mueve ni un
-- centavo de ningún informe.

ALTER TABLE scintela.proveedor
    ALTER COLUMN retbase TYPE numeric(6,2),
    ALTER COLUMN retiva  TYPE numeric(6,2);
