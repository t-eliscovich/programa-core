-- Migration 0028: cartera_snapshots + flags para auto-cierre de stock mensual.
--
-- ITEM #4 (CONTROLC). dBase MENU.PRG L1582-1660 leía la cartera desde
-- carpetas día-de-la-semana (F:\LUNES\FACTURAS, F:\MARTES\..., etc.) y la
-- comparaba contra la cartera actual. En PG no hay carpetas → tabla.
--
-- ITEM #5 (auto-cierre de stock mensual). Reusamos scintela.sistema_meta
-- (migración 0026) con clave 'cierre_mes_ult_fecha' para evitar doble
-- corrida del job que copia HI/TJ/PF/UM/UK/UQ/UF del mes en curso al
-- nuevo (replica MENU.PRG L246-263).
--
-- Idempotente.

-- ---------------------------------------------------------------------------
-- 1. Snapshots de cartera (ITEM #4)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scintela.cartera_snapshots (
    id_snapshot    SERIAL PRIMARY KEY,
    fecha          DATE       NOT NULL,
    codigo_cli     VARCHAR(5) NOT NULL,
    saldo_total    NUMERIC(14, 2),
    n_facturas     INT,
    snapshot_ts    TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT cartera_snapshots_uq UNIQUE (fecha, codigo_cli)
);

COMMENT ON TABLE scintela.cartera_snapshots IS
    'Snapshot diario de cartera por cliente. Reemplaza el patrón legacy '
    'de F:\\LUNES\\FACTURAS de dBase (MENU.PRG L1582-1660 PROCEDURE CONTROLC). '
    'Idempotente vía UNIQUE(fecha, codigo_cli). Usado por '
    'modules.cartera.queries.comparar_contra_snapshot().';

CREATE INDEX IF NOT EXISTS cartera_snapshots_fecha_idx
    ON scintela.cartera_snapshots (fecha);

CREATE INDEX IF NOT EXISTS cartera_snapshots_codigo_cli_idx
    ON scintela.cartera_snapshots (codigo_cli);


-- ---------------------------------------------------------------------------
-- 2. Flag de cierre de stock mensual (ITEM #5)
-- ---------------------------------------------------------------------------
--
-- scintela.sistema_meta ya existe (migración 0026). Sólo inicializamos la
-- clave de cierre — vale "0000-00" (semilla) para que el primer call con
-- mes_actual > "0000-00" aplique. Idempotente: ON CONFLICT DO NOTHING.
INSERT INTO scintela.sistema_meta (clave, valor)
VALUES ('cierre_mes_ult_fecha', '1900-01')
ON CONFLICT (clave) DO NOTHING;
