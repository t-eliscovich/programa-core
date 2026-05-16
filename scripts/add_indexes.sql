-- =====================================================================
-- Programa Core — índices para consultas frecuentes
-- =====================================================================
--
-- Aplicar en producción con:
--     psql $DATABASE_URL -f scripts/add_indexes.sql
--
-- Todos son CREATE INDEX IF NOT EXISTS → seguros de re-correr.
-- CONCURRENTLY evita bloquear escrituras; requiere no estar en transacción.
-- =====================================================================

SET search_path TO scintela, public;

-- FACTURA ---------------------------------------------------------------
-- Listados se filtran y ordenan por fecha; joins usan codigo_cli.
CREATE INDEX IF NOT EXISTS idx_factura_fecha        ON scintela.factura (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_factura_codigo_cli   ON scintela.factura (codigo_cli);
CREATE INDEX IF NOT EXISTS idx_factura_stat         ON scintela.factura (stat) WHERE stat IS NOT NULL;

-- CHEQUE ----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_cheque_fecha         ON scintela.cheque (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_cheque_codigo_cli    ON scintela.cheque (codigo_cli);
CREATE INDEX IF NOT EXISTS idx_cheque_stat          ON scintela.cheque (stat);
CREATE INDEX IF NOT EXISTS idx_cheque_no_banco      ON scintela.cheque (no_banco);
-- For estado_cuenta / cartera fast lookups by (client, stat).
CREATE INDEX IF NOT EXISTS idx_cheque_cli_stat      ON scintela.cheque (codigo_cli, stat);

-- COMPRA ----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_compra_fecha         ON scintela.compra (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_compra_codigo_prov   ON scintela.compra (codigo_prov);

-- POSDAT ----------------------------------------------------------------
-- Gastos del mes, deudas por banco/fechad.
CREATE INDEX IF NOT EXISTS idx_posdat_fecha         ON scintela.posdat (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_posdat_fechad        ON scintela.posdat (fechad);
CREATE INDEX IF NOT EXISTS idx_posdat_banc          ON scintela.posdat (banc);
CREATE INDEX IF NOT EXISTS idx_posdat_prov          ON scintela.posdat (prov);

-- TRANSACCIONES_BANCARIAS ----------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tbanc_fecha          ON scintela.transacciones_bancarias (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_tbanc_no_banco       ON scintela.transacciones_bancarias (no_banco);
CREATE INDEX IF NOT EXISTS idx_tbanc_documento      ON scintela.transacciones_bancarias (documento);
CREATE INDEX IF NOT EXISTS idx_tbanc_stat           ON scintela.transacciones_bancarias (stat) WHERE stat IS NOT NULL;

-- CAJA ------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_caja_fecha           ON scintela.caja (fecha DESC);

-- CAPITAL ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_capital_fecha        ON scintela.capital (fecha DESC);

-- RETENCION -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_retencion_fecha      ON scintela.retencion (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_retencion_codigo_cli ON scintela.retencion (codigo_cli);

-- FLUJO (ya PK en fecha, pero por si acaso) ----------------------------
CREATE INDEX IF NOT EXISTS idx_flujo_fecha          ON scintela.flujo (fecha DESC);

-- CLIENTE / PROVEEDOR — lookups alfabéticos y activos
-- NOTA: scintela.cliente NO tiene columna `activo` (usa `stop` char(1) para bloquear).
--       scintela.proveedor SÍ tiene `activo` (varchar(2), valores '1'/'0').
CREATE INDEX IF NOT EXISTS idx_cliente_nombre       ON scintela.cliente (UPPER(nombre));
CREATE INDEX IF NOT EXISTS idx_cliente_stop         ON scintela.cliente (stop) WHERE stop IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proveedor_nombre     ON scintela.proveedor (UPPER(nombre));
CREATE INDEX IF NOT EXISTS idx_proveedor_activo     ON scintela.proveedor (activo);

-- CHEQUESXFACT / CHEQUEXTRANSACCION — joins desde detalle
CREATE INDEX IF NOT EXISTS idx_cxf_id_fact          ON scintela.chequesxfact (id_fact);
CREATE INDEX IF NOT EXISTS idx_cxf_id_cheque        ON scintela.chequesxfact (id_cheque);
CREATE INDEX IF NOT EXISTS idx_cxt_id_cheque        ON scintela.chequextransaccion (id_cheque);
CREATE INDEX IF NOT EXISTS idx_cxt_id_transaccion   ON scintela.chequextransaccion (id_transaccion);

-- SEGURIDAD -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_usuario_username     ON seguridad.usuario (username);
CREATE INDEX IF NOT EXISTS idx_usuario_activo       ON seguridad.usuario (activo);

-- ANALYZE para que el planner use los nuevos índices inmediatamente.
ANALYZE scintela.factura;
ANALYZE scintela.cheque;
ANALYZE scintela.compra;
ANALYZE scintela.posdat;
ANALYZE scintela.transacciones_bancarias;
ANALYZE scintela.caja;
ANALYZE scintela.capital;
ANALYZE scintela.retencion;
ANALYZE scintela.cliente;
ANALYZE scintela.proveedor;
