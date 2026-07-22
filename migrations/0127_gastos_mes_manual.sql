-- Migration 0127: scintela.gastos_mes_manual — override MANUAL de los gastos
-- del mes por rubro (tej/tin/adm), una fila por período 'YYYY-MM'.
--
-- Pedido Federico 2026-07-22: la fila "Gastos mes anterior" de /informes/gastos
-- se calcula de xgast + compras + amortización, pero los meses ANTERIORES a
-- julio 2026 no tienen gastos cargados en xgast → la fila sale mal (ej. junio
-- Administración ~3.950, sólo amortización). Con esta tabla se pueden FORZAR
-- los valores reales del mes por rubro, COMPARTIDOS por todos los usuarios. Si
-- hay fila para el período, la vista la usa en lugar del cálculo automático.
--
-- Tabla NUEVA, fuera del TABLE_MAP del sync del dBase → el sync no la toca.
-- Idempotente.

CREATE TABLE IF NOT EXISTS scintela.gastos_mes_manual (
    periodo          TEXT PRIMARY KEY,               -- 'YYYY-MM'
    tej              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    tin              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    adm              NUMERIC(14, 2) NOT NULL DEFAULT 0,
    usuario_modifica TEXT,
    fecha_modifica   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE scintela.gastos_mes_manual IS
    'Override manual de los gastos del mes por rubro (tej/tin/adm), una fila por '
    'período YYYY-MM. Lo usa la fila "Gastos mes anterior" de /informes/gastos '
    'para FORZAR el valor cuando el cálculo automático no sirve (meses sin datos '
    'en xgast). Compartido por todos los usuarios. No forma parte del sync del dBase.';

-- Seed junio 2026 (pedido Federico): forzar los gastos del mes.
-- Tejeduría 120.000 · Tintorería 360.000 · Administración 295.000 · Total 775.000.
INSERT INTO scintela.gastos_mes_manual (periodo, tej, tin, adm, usuario_modifica)
VALUES ('2026-06', 120000, 360000, 295000, 'seed-migracion-0127')
ON CONFLICT (periodo) DO NOTHING;
