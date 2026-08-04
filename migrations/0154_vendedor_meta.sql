-- Migración 0154: metas de venta por vendedor y mes.
--
-- TMT 2026-08-03 (dueña, sobre el portal de vendedores): "meta semanal,
-- mensual, anual" y después "me gusta como lo mostrás como ventas arriba y
-- luego cobrado" → la meta es de VENTAS FACTURADAS; la cobranza va como dato
-- de apoyo abajo.
--
-- Se carga UNA meta por vendedor y por MES. Las otras dos se derivan:
--     meta del AÑO    = suma de las 12 mensuales cargadas
--     meta de SEMANA  = meta del mes × 7 / días del mes (prorrateo)
-- Pedirle 52 números por vendedor por año a la dueña sería garantizar que la
-- pantalla quede vacía.
--
-- Sin meta cargada la pantalla NO muestra el anillo de progreso — muestra el
-- facturado a secas. Un 0% falso es peor que no mostrar nada.
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS scintela.vendedor_meta (
    codigo            VARCHAR(3)   NOT NULL,
    anio              INTEGER      NOT NULL,
    mes               INTEGER      NOT NULL CHECK (mes BETWEEN 1 AND 12),
    monto             NUMERIC(14,2) NOT NULL DEFAULT 0,
    usuario_actualiza VARCHAR(30),
    fecha_actualiza   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (codigo, anio, mes)
);

COMMENT ON TABLE scintela.vendedor_meta IS
    'Meta de VENTAS FACTURADAS por vendedor y mes. La anual se suma y la '
    'semanal se prorratea — no se cargan aparte. Sin fila = sin meta.';

-- Igual que scintela.vendedor: si el runner corre como postgres y la app usa
-- otro user, pasarle ownership (ver migración 0032).
DO $$
DECLARE
    app_user TEXT := current_user;
BEGIN
    IF app_user <> 'postgres' THEN
        EXECUTE format('ALTER TABLE scintela.vendedor_meta OWNER TO %I', app_user);
    END IF;
END $$;
