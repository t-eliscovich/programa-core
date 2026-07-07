-- 0113_importacion_pago_mov.sql
-- TMT 2026-07-06 (dueña) — modelo SIMPLIFICADO de importaciones:
--   "Ya no hace falta la división de pagado o no. Dejamos de PREDECIR cuánto
--    saldría: los anticipos son casi el 90% y lo RESTANTE se carga en /compras."
--
-- 1. Una importación puede tener MUCHOS anticipos, cargados en cualquier
--    momento. Hoy scintela.importacion_pago tiene UNA sola columna
--    anticipo_aplicado por importación → cargar un 2º anticipo hacía UPDATE
--    y PISABA el 1º (le pasó a la dueña el 06/07). Nada se sobrescribe más:
--    cada carga es un MOVIMIENTO nuevo en esta tabla, y anticipo_aplicado
--    pasa a ser CACHE derivado (Σ movimientos), actualizado por
--    modules/importaciones/pago.py en cada alta/baja.
-- 2. VALOR DEL STOCK de la importación = Σ anticipos pagados (decisión
--    explícita). costo_estimado/deuda/pagada/monto_real quedan en
--    importacion_pago SOLO como referencia histórica: nada los lee.
-- 3. Cada movimiento genera además una NOTA DE DÉBITO automática en Pichincha
--    (id_transaccion la linkea; la dueña la hacía a mano). El ✕ de la UI borra
--    el movimiento Y compensa su ND (par atómico, mov_doble de auditoría).
--
-- La UI solo carga tipo='anticipo'; el CHECK admite también 'pago' por si
-- acaso (decisión explícita: no cerrar la puerta en el schema).
--
-- PC-only: NO va al TABLE_MAP del Sync dBase → sobrevive intacta el
-- TRUNCATE+INSERT de las tablas espejo (mismo patrón que
-- scintela.importacion_pago mig 0104 y scintela.op_retiro_linea mig 0109).
-- Idempotente y re-corrible (el backfill solo corre si la tabla está vacía).
DO $$
BEGIN
    IF to_regclass('scintela.importacion_pago') IS NULL THEN
        RAISE EXCEPTION 'Falta la migración 0104 (scintela.importacion_pago).';
    END IF;

    IF to_regclass('scintela.importacion_pago_mov') IS NULL THEN
        CREATE TABLE scintela.importacion_pago_mov (
            id_mov         BIGSERIAL PRIMARY KEY,
            im_numero      VARCHAR(20)   NOT NULL,
            tipo           VARCHAR(10)   NOT NULL
                           CHECK (tipo IN ('anticipo', 'pago')),
            fecha          DATE          NOT NULL,
            monto          NUMERIC(14,2) NOT NULL,
            nota           VARCHAR(120),
            -- ND automática en Pichincha (transacciones_bancarias). NULL en
            -- los movimientos del backfill (esas ND las hizo la dueña a mano
            -- en su momento — NO hay que duplicarlas).
            id_transaccion BIGINT,
            usuario_crea   VARCHAR(50),
            fecha_crea     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'scintela'
           AND indexname  = 'ix_importacion_pago_mov_im'
    ) THEN
        CREATE INDEX ix_importacion_pago_mov_im
            ON scintela.importacion_pago_mov (im_numero);
    END IF;

    -- ── BACKFILL (solo si la tabla estaba vacía — idempotente) ────────────
    -- El nuevo criterio de valuación (stock = Σ anticipos, ya no el costo
    -- estimado) aplica HACIA ADELANTE: para que el valor de las
    -- importaciones existentes NO SALTE en el deploy, cada fila vieja recibe
    -- UN movimiento 'anticipo' inicial por su VALOR ACTUAL EFECTIVO según
    -- la semántica vieja (migs 0104/0107):
    --   · pagada = TRUE  → el valor efectivo era el costo total real:
    --                      monto_real (o costo_estimado si no lo cargaron).
    --   · si no, con anticipo_aplicado > 0 → lo efectivamente pagado hasta
    --                      hoy: anticipo_aplicado (las recibidas-no-pagadas
    --                      pasan a valer LO PAGADO — es el cambio querido, y
    --                      así ya venían neteadas en el flujo de anticipos).
    --   · sin pago ni anticipo → sin movimiento (valor 0 = nada pagado aún).
    -- Sin ND linkeada (id_transaccion NULL): esas ND ya existen en el banco
    -- porque la dueña las cargaba a mano — NO hay que duplicarlas (el ✕ de
    -- estos movimientos tampoco genera NC).
    IF NOT EXISTS (SELECT 1 FROM scintela.importacion_pago_mov) THEN
        INSERT INTO scintela.importacion_pago_mov
            (im_numero, tipo, fecha, monto, nota, usuario_crea)
        SELECT im_numero, 'anticipo',
               COALESCE(fecha_pago, fecha_recepcion_pc, fecha_modifica::date,
                        fecha_crea::date, CURRENT_DATE),
               ROUND(CASE
                         WHEN COALESCE(pagada, FALSE)
                          AND COALESCE(monto_real, costo_estimado, 0) > 0
                             THEN COALESCE(monto_real, costo_estimado)
                         ELSE COALESCE(anticipo_aplicado, 0)
                     END, 2),
               'backfill mig 0113 (valor efectivo al deploy)', 'mig-0113'
          FROM scintela.importacion_pago
         WHERE im_numero IS NOT NULL
           AND ROUND(CASE
                         WHEN COALESCE(pagada, FALSE)
                          AND COALESCE(monto_real, costo_estimado, 0) > 0
                             THEN COALESCE(monto_real, costo_estimado)
                         ELSE COALESCE(anticipo_aplicado, 0)
                     END, 2) > 0;

        -- Cache derivado consistente con los movimientos recién creados:
        -- anticipo_aplicado = Σ movimientos (= valor del stock);
        -- deuda = NULL (dejó de existir como concepto — el pasivo real vive
        -- en posdat vía /compras). pagada/monto_real NO se tocan (referencia
        -- histórica congelada del modelo v1).
        UPDATE scintela.importacion_pago p
           SET anticipo_aplicado = COALESCE(m.total, 0),
               deuda = NULL
          FROM (SELECT im_numero, SUM(monto) AS total
                  FROM scintela.importacion_pago_mov
                 GROUP BY im_numero) m
         WHERE m.im_numero = p.im_numero;

        UPDATE scintela.importacion_pago
           SET anticipo_aplicado = 0, deuda = NULL
         WHERE im_numero NOT IN (SELECT im_numero
                                   FROM scintela.importacion_pago_mov);
    END IF;
END $$;
