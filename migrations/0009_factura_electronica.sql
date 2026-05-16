-- =====================================================================
-- 0009_factura_electronica
-- =====================================================================
-- Soporte para facturación electrónica del SRI (Servicio de Rentas
-- Internas de Ecuador). Este es el MVP del módulo SRI — sólo persistencia
-- del XML generado y trazabilidad del ciclo de vida contra el SRI.
-- No hace firma electrónica ni envío SOAP todavía (ver modules/sri/*).
--
-- Alcance de esta migración:
--   - Crear scintela.factura_electronica (1:1 con scintela.factura).
--   - Índice único sobre clave_acceso (49 dígitos — identidad SRI).
--   - Índice por id_factura (JOIN con la cabecera).
--   - Índice parcial por estado != 'autorizado' (cola de pendientes).
--
-- Se NO agrega FK dura a scintela.factura porque:
--   (a) scintela.factura tiene PK pero id_factura no tiene default en el
--       dump legacy — evitamos romper migraciones viejas.
--   (b) una factura anulada (stat='Y') debe poder mantener su registro
--       electrónico (si fue autorizada, no se borra — se emite nota de
--       crédito).
-- La integridad se mantiene en código (modules/sri/queries.py).
--
-- Idempotente: CREATE TABLE IF NOT EXISTS.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.factura_electronica (
    id_factura_electronica BIGSERIAL PRIMARY KEY,

    -- Ligazón con la factura de venta (1:1).
    id_factura              integer      NOT NULL,

    -- Identidad SRI: la clave de acceso de 49 dígitos es el id único
    -- del comprobante en todo el ecosistema SRI. Se calcula en app
    -- (modules/sri/core.py::generar_clave_acceso) antes del INSERT.
    clave_acceso            varchar(49)  NOT NULL,

    -- Ambiente SRI:
    --   '1' = certificación (pruebas, celcer.sri.gob.ec) — default
    --   '2' = producción     (real,     cel.sri.gob.ec)
    ambiente                char(1)      NOT NULL DEFAULT '1'
        CHECK (ambiente IN ('1', '2')),

    -- Tipo de emisión:
    --   '1' = normal (en línea con SRI)
    --   '2' = contingencia (raramente usado — offline, sube después)
    tipo_emision            char(1)      NOT NULL DEFAULT '1'
        CHECK (tipo_emision IN ('1', '2')),

    -- Tipo de comprobante (los que nos importan hoy):
    --   '01' = factura
    --   '04' = nota de crédito
    --   '05' = nota de débito
    --   '06' = guía de remisión
    --   '07' = comprobante de retención
    tipo_comprobante        varchar(2)   NOT NULL DEFAULT '01',

    -- Desglose del "número de comprobante" que el usuario ve (001-001-000000123).
    estab                   varchar(3)   NOT NULL DEFAULT '001',
    pto_emi                 varchar(3)   NOT NULL DEFAULT '001',
    secuencial              varchar(9)   NOT NULL,

    -- Fecha contable de la factura (YYYYMMDD en clave de acceso).
    fecha_emision           date         NOT NULL,

    -- Totales — snapshot al momento de generar el XML. Si la factura
    -- origen se modifica, este registro mantiene lo emitido.
    subtotal_sin_impuestos  numeric(12,2) NOT NULL DEFAULT 0,
    subtotal_iva_0          numeric(12,2) NOT NULL DEFAULT 0,
    subtotal_iva_grav       numeric(12,2) NOT NULL DEFAULT 0,
    iva_porcentaje          numeric(5,2)  NOT NULL DEFAULT 15.00,
    total_iva               numeric(12,2) NOT NULL DEFAULT 0,
    importe_total           numeric(12,2) NOT NULL DEFAULT 0,

    -- Ciclo de vida SRI.
    --   'borrador'    — XML generado, sin firmar. Se puede regenerar.
    --   'firmado'     — firmado con el .p12, listo para enviar.
    --   'enviado'     — enviado a SRI, esperando autorización (estado fugaz).
    --   'autorizado'  — SRI respondió AUTORIZADO. Es factura fiscal real.
    --   'rechazado'   — SRI respondió DEVUELTA o NO AUTORIZADO. Ver mensaje_error.
    --   'anulado'     — factura anulada después de autorización (requiere nota crédito).
    estado                  varchar(20)  NOT NULL DEFAULT 'borrador'
        CHECK (estado IN ('borrador','firmado','enviado','autorizado','rechazado','anulado')),

    -- Respuesta cruda del SRI — guardamos todo para auditoría y debugging.
    numero_autorizacion     varchar(49),    -- en la mayoría de casos == clave_acceso
    fecha_autorizacion      timestamp,
    xml_generado            text         NOT NULL,
    xml_firmado             text,
    respuesta_sri           jsonb,           -- mensajes/informacionAdicional del SRI
    mensaje_error           text,            -- resumen legible para mostrar al usuario

    -- Trazabilidad de envío.
    intentos                integer      NOT NULL DEFAULT 0,
    ultimo_intento_en       timestamp,

    -- Audit columns — mismo patrón que todo scintela.*.
    fecha_crea              timestamp    DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica          timestamp,
    usuario_crea            varchar(50),
    usuario_modifica        varchar(50)
);

-- Identidad SRI: clave_acceso es globalmente única (distinta por RUC,
-- distinta por secuencial, distinta por ambiente). Un duplicado aquí
-- indica que estamos re-emitiendo el mismo comprobante, lo que no es
-- posible en producción — fallar fuerte.
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_electronica_clave_acceso
    ON scintela.factura_electronica (clave_acceso);

-- Lookup más común: "dame el registro SRI de esta factura".
CREATE INDEX IF NOT EXISTS idx_factura_electronica_id_factura
    ON scintela.factura_electronica (id_factura);

-- Cola de pendientes — todo lo que no está 'autorizado' ni 'anulado'.
-- Parcial para no crecer con el histórico autorizado (>95% del total).
CREATE INDEX IF NOT EXISTS idx_factura_electronica_pendientes
    ON scintela.factura_electronica (estado, fecha_emision DESC)
    WHERE estado NOT IN ('autorizado', 'anulado');

-- Orden temporal para informes.
CREATE INDEX IF NOT EXISTS idx_factura_electronica_fecha_emision
    ON scintela.factura_electronica (fecha_emision DESC);

-- Documentación inline — el próximo que abra el schema lee esto y
-- entiende por qué hay tantos campos.
COMMENT ON TABLE scintela.factura_electronica IS
    'Registro 1:1 con scintela.factura para el ciclo de vida de facturación electrónica SRI Ecuador. '
    'Almacena XML generado/firmado, clave de acceso de 49 dígitos, estado del comprobante '
    '(borrador → firmado → enviado → autorizado|rechazado) y la respuesta cruda del SRI para auditoría.';

COMMENT ON COLUMN scintela.factura_electronica.clave_acceso IS
    'Identidad SRI de 49 dígitos. Se calcula con modules/sri/core.py::generar_clave_acceso. '
    'Módulo 11 en el último dígito. No cambia después de crear el registro.';

COMMENT ON COLUMN scintela.factura_electronica.ambiente IS
    '1=certificación (sandbox SRI), 2=producción. Default 1. Cambiar a 2 sólo cuando el RUC '
    'tenga la habilitación correspondiente y el .p12 sea de firma real.';

COMMENT ON COLUMN scintela.factura_electronica.estado IS
    'Ciclo de vida del comprobante: borrador → firmado → enviado → autorizado|rechazado. '
    'Un comprobante autorizado NO se edita — se emite nota de crédito si hace falta revertir.';
