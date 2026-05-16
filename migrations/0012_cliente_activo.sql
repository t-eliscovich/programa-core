-- =====================================================================
-- 0012_cliente_activo
-- =====================================================================
-- Agrega columna `activo` a scintela.cliente para soft-delete.
-- Reemplaza la opción `MODIFICA.PRG > DIFUNTOS` del legacy: marcar un
-- cliente como inactivo sin borrarlo del histórico (las facturas viejas
-- se mantienen).
--
-- Default TRUE para no romper datos existentes. Las queries filtran por
-- activo solo cuando aplica (la lista, el directorio, los autocompletes);
-- los reportes históricos (cartera, estado de cuenta) NO filtran porque
-- pueden tener saldos vivos de clientes ya inactivados.
-- =====================================================================

ALTER TABLE scintela.cliente
    ADD COLUMN IF NOT EXISTS activo boolean NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_cliente_activo
    ON scintela.cliente (activo)
    WHERE activo = FALSE;

COMMENT ON COLUMN scintela.cliente.activo IS
    'TRUE = cliente vivo. FALSE = "difunto" (legacy DIFUNTOS): no se muestra '
    'en autocompletes ni listas por default, pero se preservan sus facturas '
    'históricas y aparece en cartera si tiene saldos vivos.';
