-- 0109_op_retiro_linea.sql
-- TMT 2026-06-30 (dueña): el retiro a accionistas (OP) ya no se carga con un
-- form libre — tiene que salir DESDE UNA LÍNEA OP YA REGISTRADA (una compra OP
-- de importación, o la fila OP de posdatados) y esa línea tiene que BAJAR al
-- retirar. Pero el balance debe contarse UNA SOLA VEZ.
--
-- Solución sin doble conteo: el retiro sigue yendo a scintela.retiros (de='OP')
-- — ESE es el único asiento que pega en el balance (URET), igual que hoy. Esta
-- tabla es SÓLO un mapeo display: a qué línea OP se imputó cada retiro, para
-- mostrar el "saldo restante por línea" (= crédito de la línea − Σ imputado).
-- NO la lee el balance. El crédito guardado (compra/posdat OP) NO se edita.
--
-- line_key = identidad ESTABLE de la línea a través del Sync dBase:
--   compra OP  -> 'C|' || numero || '|' || concepto
--   posdat OP  -> 'P|' || num    || '|' || concepto
-- (numero/num y concepto vienen del DBF, vuelven identicos tras el TRUNCATE).
--
-- NO esta en el TABLE_MAP del sync -> sobrevive intacta (igual que
-- scintela.importacion_pago, mig 0104). id_retiro se guarda solo como
-- referencia (el sync reasigna ids a los retiros pc-retiro-op, por eso el
-- vinculo durable es line_key + monto, no id_retiro).
-- Idempotente y re-corrible.
DO $$
BEGIN
    IF to_regclass('scintela.op_retiro_linea') IS NULL THEN
        CREATE TABLE scintela.op_retiro_linea (
            id_op_retiro_linea BIGSERIAL PRIMARY KEY,
            line_key      TEXT          NOT NULL,
            fecha         DATE          NOT NULL,
            monto         NUMERIC(14,2) NOT NULL,
            id_retiro     INTEGER,
            concepto      VARCHAR(120),
            usuario_crea  VARCHAR(50),
            fecha_crea    TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'scintela'
           AND indexname  = 'ix_op_retiro_linea_key'
    ) THEN
        CREATE INDEX ix_op_retiro_linea_key
            ON scintela.op_retiro_linea (line_key);
    END IF;
END $$;
