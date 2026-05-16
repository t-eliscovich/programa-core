-- 0007_ejecuciones_tareas.sql
--
-- Tracker idempotente de tareas programadas (ejecución mensual de procedures
-- PostgreSQL que antes corrían "a mano" desde el dBase: procesa_provisiones,
-- actualizar_amortizacion, etc.).
--
-- Invariante: una tarea + un periodo ('YYYY-MM') se ejecuta como máximo una
-- vez con estado final 'O' (ok). UNIQUE(tarea, periodo) lo hace cumplir.
-- El runner hace INSERT ... ON CONFLICT DO NOTHING RETURNING id_ejecucion
-- para adueñarse del slot; si no devuelve id, ya corrió (o está corriendo)
-- y el proceso actual sale sin tocar nada.

CREATE TABLE IF NOT EXISTS scintela.ejecuciones_tareas (
    id_ejecucion   BIGSERIAL PRIMARY KEY,
    tarea          VARCHAR(60) NOT NULL,
    periodo        CHAR(7) NOT NULL,                        -- formato 'YYYY-MM'
    iniciado_en    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    terminado_en   TIMESTAMP,
    estado         CHAR(1) NOT NULL DEFAULT 'R',            -- R(unning) / O(k) / E(rror)
    mensaje        TEXT,
    host           VARCHAR(60),
    CONSTRAINT uq_ejecuciones_tareas_periodo UNIQUE (tarea, periodo),
    CONSTRAINT ck_ejecuciones_tareas_estado  CHECK (estado IN ('R','O','E')),
    CONSTRAINT ck_ejecuciones_tareas_periodo CHECK (periodo ~ '^[0-9]{4}-[0-9]{2}$')
);

CREATE INDEX IF NOT EXISTS idx_ejecuciones_tareas_periodo
    ON scintela.ejecuciones_tareas (periodo DESC, tarea);

CREATE INDEX IF NOT EXISTS idx_ejecuciones_tareas_estado
    ON scintela.ejecuciones_tareas (estado, iniciado_en DESC)
    WHERE estado <> 'O';
