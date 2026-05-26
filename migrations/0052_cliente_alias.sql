-- 0052_cliente_alias.sql
-- TMT 2026-05-26 dueña: Asinfo y PC tienen códigos distintos para el mismo
-- cliente. Conocidos al hablar con la dueña hoy:
--   CL2 (Asinfo) ↔ CLR (PC)
--   AJ2 (Asinfo) ↔ AJO (PC)
--   J3C (Asinfo) ↔ VGA (PC)
-- Quedan VP1/VPM que la dueña dice que son DISTINTOS clientes (no alias).
--
-- Esta tabla mapea sus códigos. Cuando crucemos Asinfo↔PC (backfill de
-- numf_completo, audit huerfanas, "asinfo no en pc"), antes de comparar
-- codigo_cli aplicamos la alias map.
--
-- Direccional: `codigo_asinfo` es el que viene de Metabase / ASINFO_CARD_FACTURAS;
-- `codigo_pc` es el que tiene scintela.factura.codigo_cli y scintela.cliente.
-- Si la dueña agrega más aliases en el futuro (UI o INSERT manual), se
-- aplican automáticamente — no requieren redeploy de código.

CREATE TABLE IF NOT EXISTS scintela.cliente_alias (
    codigo_asinfo TEXT NOT NULL,
    codigo_pc     TEXT NOT NULL,
    nota          TEXT,
    fecha_alta    DATE NOT NULL DEFAULT CURRENT_DATE,
    usuario_crea  TEXT,
    PRIMARY KEY (codigo_asinfo, codigo_pc)
);

CREATE INDEX IF NOT EXISTS idx_cliente_alias_asinfo
    ON scintela.cliente_alias (codigo_asinfo);
CREATE INDEX IF NOT EXISTS idx_cliente_alias_pc
    ON scintela.cliente_alias (codigo_pc);

-- Seed con los 3 conocidos. ON CONFLICT DO NOTHING para idempotencia
-- (la migración se puede re-correr).
INSERT INTO scintela.cliente_alias (codigo_asinfo, codigo_pc, nota, usuario_crea)
VALUES
    ('CL2', 'CLR', 'CL2 es subcliente de CLR — dueña 2026-05-26', 'migration_0052'),
    ('AJ2', 'AJO', 'AJ2 es subcliente de AJO — dueña 2026-05-26', 'migration_0052'),
    ('J3C', 'VGA', 'J3C corresponde a VGA — dueña 2026-05-26',    'migration_0052')
ON CONFLICT (codigo_asinfo, codigo_pc) DO NOTHING;
