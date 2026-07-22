-- Migration 0126: scintela.gastos_proyectado_mes — gastos proyectados por rubro.
--
-- Pedido dueña 2026-07-22: los casilleros "Gastos proyectados este mes" de
-- /informes/gastos (agregados por Federico el 21/07) guardaban en localStorage
-- del navegador → cada usuario veía sólo lo suyo y no persistía ("no se guarda
-- lo que yo o Federico ponemos"). Ahora se guardan en la base, compartidos por
-- todos los usuarios y por período (YYYY-MM).
--
-- Tabla NUEVA, fuera del TABLE_MAP del sync del dBase → el TRUNCATE+INSERT del
-- sync NO la toca (análogo a por qué las ediciones web se perdían en iniciales).
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS scintela.gastos_proyectado_mes (
    periodo          TEXT PRIMARY KEY,               -- 'YYYY-MM'
    tej              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    tin              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    adm              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    usuario_modifica TEXT,
    fecha_modifica   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE scintela.gastos_proyectado_mes IS
    'Gastos proyectados por rubro (tej/tin/adm) para el mes, editables desde '
    '/informes/gastos. Compartidos por todos los usuarios; una fila por período '
    'YYYY-MM. No forma parte del sync del dBase.';
