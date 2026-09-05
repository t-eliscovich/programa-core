-- =====================================================================
-- 0244 · La foto de la traza guarda DE QUÉ está hecho el $/kg del hilado
-- =====================================================================
-- Tamara 05/09/2026: *"asegurate de tener todas las variables del proceso
-- trackeadas"*. Hasta hoy la foto guardaba compras_kg / compras_us como un
-- total, y un salto del $/kg salía como "cambió el $/kg de 3 etapas" sin
-- decir por qué. Ahora cada foto guarda el desglose de ese total (qué entró
-- por importación recibida, por compra local, por el botón "Entrar al precio
-- del hilo" y por recargos tardíos) y, en `hilado_insumos`, el detalle con
-- nombres (los recargos con su importación) más la apertura y los kilos
-- con los que se armó el promedio. Con eso la traza y la nota del día pueden
-- decir "entraron los recargos de AC 39 y MD 1 (+6.849,51)".
-- =====================================================================

ALTER TABLE scintela.traza_utilidad
    ADD COLUMN IF NOT EXISTS compras_import_us    NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS compras_local_us     NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS al_precio_us         NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS recargos_tardios_us  NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS hilado_insumos       JSONB;

COMMENT ON COLUMN scintela.traza_utilidad.compras_import_us   IS 'US$ de importaciones recibidas en el mes (con recargos del mismo mes)';
COMMENT ON COLUMN scintela.traza_utilidad.compras_local_us    IS 'US$ de compras locales de hilo recibidas en el mes (kg × tarifa)';
COMMENT ON COLUMN scintela.traza_utilidad.al_precio_us        IS 'US$ de compras marcadas "Entrar al precio del hilo" (mig 0241)';
COMMENT ON COLUMN scintela.traza_utilidad.recargos_tardios_us IS 'US$ de recargos del mes de importaciones recibidas en meses anteriores';
COMMENT ON COLUMN scintela.traza_utilidad.hilado_insumos      IS 'Detalle: recargos con su importación, apertura, kg con/sin costo';
