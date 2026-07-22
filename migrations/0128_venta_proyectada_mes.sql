-- Migration 0128: scintela.venta_proyectada_mes — kg de VENTA PROYECTADA por
-- mes (editable desde la fila "Proyección" del Informe de Resultados / balance).
--
-- Pedido Federico 2026-07-22: el kg de la Proyección (ej. 320.000) venía fijo de
-- `scintela.iniciales` (KGPRO). Ahora se puede EDITAR desde el balance y el valor
-- se guarda acá, COMPARTIDO por todos los usuarios. Alimenta la Utilidad
-- Proyectada (venta proyectada = kg × precio, y costos directos = kg × (MP +
-- Colorantes) × 1,05). Si hay fila para el período, la vista la usa en lugar del
-- KGPRO de iniciales.
--
-- Tabla NUEVA, fuera del TABLE_MAP del sync del dBase → el sync no la toca
-- (igual que gastos_proyectado_mes / gastos_mes_manual). Idempotente.

CREATE TABLE IF NOT EXISTS scintela.venta_proyectada_mes (
    periodo          TEXT PRIMARY KEY,               -- 'YYYY-MM'
    kg               NUMERIC(14, 2) NOT NULL DEFAULT 0,
    usuario_modifica TEXT,
    fecha_modifica   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE scintela.venta_proyectada_mes IS
    'Kg de venta proyectada del mes (una fila por período YYYY-MM), editable '
    'desde la fila Proyección del balance. Si existe, PISA el KGPRO de iniciales. '
    'Compartido por todos los usuarios. No forma parte del sync del dBase.';
