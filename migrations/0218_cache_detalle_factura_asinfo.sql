-- Caché del detalle de facturas de Asinfo (qué se llevó el cliente)
--
-- TMT 2026-08-25 (dueña): *"el que se llevó carga lento"*. Medido en
-- producción: 630-780 ms cada vez. Y no es la consulta — le pregunté a Asinfo
-- la cosa más tonta que se puede preguntar (las columnas de una tabla) y tardó
-- 590-690 ms igual. Ese es el PEAJE FIJO de cruzar el puente de Metabase hasta
-- el SQL Server, y ninguna reescritura del SQL lo baja.
--
-- La única forma de que sea rápido es no volver a preguntar. Una factura
-- emitida no cambia nunca, así que su detalle se guarda acá: 650 ms la primera
-- vez que alguien la mira, y milésimas para todos los que vengan después —
-- incluido después de un deploy, que es justo lo que la caché en memoria no
-- puede sostener.
--
-- `datos` guarda el resultado ya AGRUPADO (tela · código · color · calidad ·
-- rollos · kilos · precio · total, con sus totales), no los renglones crudos:
-- se guarda la respuesta a la pregunta, no la materia prima.
--
-- ⚠ Es una CACHÉ: se puede vaciar entera sin perder nada. Si una factura se
-- anula y se reemite con el mismo número, la fila queda vieja hasta que la
-- pisa la precarga, que reescribe los últimos días cada media hora.
CREATE TABLE IF NOT EXISTS scintela.factura_detalle (
    numero      varchar(24) PRIMARY KEY,
    datos       jsonb       NOT NULL,
    fecha_crea  timestamptz NOT NULL DEFAULT now()
);
