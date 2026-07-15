-- Migration 0124: scintela.gasto_forzado.prov (columna PR).
--
-- Pedido dueña 2026-07-15 (sesión gastos forzados): "nos falta agregar un PR
-- ahora que veo — en el agregar". Los gastos forzados manuales ahora se
-- muestran en la misma tabla que los banc=9 (que sí tienen PR/proveedor), así
-- que el form de alta y las filas necesitan su propio campo PR. Igual que en
-- posdat, es un código corto (hasta 5 chars). Opcional (puede quedar vacío).
--
-- Idempotente.

ALTER TABLE scintela.gasto_forzado
    ADD COLUMN IF NOT EXISTS prov VARCHAR(5);

COMMENT ON COLUMN scintela.gasto_forzado.prov IS
    'Código de proveedor / PR (corto, hasta 5 chars), opcional. Alinea los '
    'gastos forzados manuales con las filas banc=9 en la pantalla de flujo.';
