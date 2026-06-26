-- 0104_importacion_pago.sql
-- TMT 2026-06-26 (dueña): "las importaciones que vienen de asinfo todavía no
-- están todas pagas. Hasta que no pongamos el monto pagado en el programa los
-- kilos aparecen como pendientes."
--
-- Estado de pago/contabilización de cada importación, keyed por la IDENTIDAD
-- de negocio (codigo_prov + número de la Nota Asinfo), NO por id_compra:
-- scintela.compra se TRUNCATE+INSERT entero en cada Sync dBase (tabla 1:1 con
-- COMPRAS.DBF), así que id_compra se reinicia y cualquier columna PC en compra
-- se borraría. Esta tabla NO está en el TABLE_MAP del sync → sobrevive intacta.
--
-- Semántica:
--   contabilizada = FALSE → la importación es "pendiente": sus kilos pueden
--                            mostrarse aparte / restarse del stock disponible.
--   contabilizada = TRUE  → la dueña marcó "ok, está el total" → libera todo.
--   monto_pagado          → informativo (permite parcial); no libera kilos por
--                            sí solo (la liberación es por la marca manual).
-- Idempotente y re-corrible.
DO $$
BEGIN
    IF to_regclass('scintela.importacion_pago') IS NULL THEN
        CREATE TABLE scintela.importacion_pago (
            id_importacion_pago BIGSERIAL PRIMARY KEY,
            codigo_prov         VARCHAR(5)  NOT NULL,
            ref_num             INTEGER     NOT NULL,
            contabilizada       BOOLEAN     NOT NULL DEFAULT FALSE,
            monto_pagado        NUMERIC(14,2) NOT NULL DEFAULT 0,
            fecha_contabilizada TIMESTAMP,
            usuario_crea        VARCHAR(50),
            fecha_crea          TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
            usuario_modifica    VARCHAR(50),
            fecha_modifica      TIMESTAMP
        );
    END IF;

    -- Clave única por (proveedor, número) — un solo estado por importación.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'scintela'
           AND indexname  = 'ux_importacion_pago_prov_num'
    ) THEN
        CREATE UNIQUE INDEX ux_importacion_pago_prov_num
            ON scintela.importacion_pago (UPPER(codigo_prov), ref_num);
    END IF;
END $$;
