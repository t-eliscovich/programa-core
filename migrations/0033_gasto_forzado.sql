-- Migration 0033: scintela.gasto_forzado.
--
-- Pedido dueña 2026-05-19 (sesión v8): los gastos forzados del flujo de
-- fondos (/informes/flujo) estaban en localStorage del navegador. Eso
-- significaba que cargabas un gasto en Chrome y al abrir Safari (u otra
-- máquina) no aparecía nada. Reporte literal: "en flujo me dice sin
-- gastos forzados. asegurate de encontrarlos y mostrarmelos".
--
-- Solución: persistir en DB para que sean cross-device. El JS sigue
-- usando optimistic locking por `version` igual que antes.
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS scintela.gasto_forzado (
    id_gasto_forzado  SERIAL       PRIMARY KEY,
    fecha             DATE         NOT NULL,
    importe           NUMERIC(14,2) NOT NULL DEFAULT 0,
    concepto          VARCHAR(80),
    version           INTEGER      NOT NULL DEFAULT 1,
    creado_por        VARCHAR(30),
    creado_en         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    actualizado_en    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    actualizado_por   VARCHAR(30)
);

COMMENT ON TABLE scintela.gasto_forzado IS
    'Gastos futuros agendados que el usuario fuerza dentro del proyectado '
    'de flujo de fondos. Reemplaza el localStorage flujo_gastos_forzados_v1.';
COMMENT ON COLUMN scintela.gasto_forzado.version IS
    'Optimistic lock — incrementa con cada UPDATE; el cliente envía el '
    'expected version y el backend rechaza si difiere.';

CREATE INDEX IF NOT EXISTS gasto_forzado_fecha_idx
    ON scintela.gasto_forzado (fecha);
