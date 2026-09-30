-- Migration 0255: la foto diaria de la cartera por cliente guarda también
-- los cheques por cobrar, los rebotados y la antigüedad de la deuda.
--
-- TMT 2026-09-30 (Tamara, análisis de cobranza): para comparar a cada
-- cliente contra sí mismo ("cómo es normalmente y cómo es ahora") hace
-- falta la historia de su deuda. La tabla existía desde la 0028 pero nadie
-- la llenaba (una sola foto, del 15/05/2026). Desde esta versión la toma
-- el ciclo de fondo del servidor (modules/cartera/foto_diaria.py).
--
-- Idempotente.

ALTER TABLE scintela.cartera_snapshots
    ADD COLUMN IF NOT EXISTS cheques_por_cobrar       NUMERIC(14, 2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cheques_rebotados        NUMERIC(14, 2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dias_factura_mas_antigua INT,
    ADD COLUMN IF NOT EXISTS saldo_mas_90_dias        NUMERIC(14, 2) DEFAULT 0;

COMMENT ON COLUMN scintela.cartera_snapshots.saldo_total IS
    'Saldo de FACTURAS vivas del cliente (sin cheques). Lo lee /cartera/controlc.';
COMMENT ON COLUMN scintela.cartera_snapshots.cheques_por_cobrar IS
    'Cheques en cartera (stat Z, P, D) al momento de la foto.';
COMMENT ON COLUMN scintela.cartera_snapshots.cheques_rebotados IS
    'Cheques protestados sin reemplazar (stat 1, 2, 3, R, 9).';
COMMENT ON COLUMN scintela.cartera_snapshots.dias_factura_mas_antigua IS
    'Días desde la FECHA (no el vencimiento) de la factura impaga más vieja.';
COMMENT ON COLUMN scintela.cartera_snapshots.saldo_mas_90_dias IS
    'Saldo de facturas con más de 90 días desde su fecha.';
