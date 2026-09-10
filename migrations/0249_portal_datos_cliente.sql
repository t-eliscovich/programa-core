-- =====================================================================
-- 0249 · Los datos que el cliente carga en su primer ingreso al portal
-- =====================================================================
-- Dueña 10/09/2026: "al momento de crearse el usuario pedirle que nos
-- detalle MAIL para envío de facturas, CELULAR y CONVENCIONAL, DIRECCIÓN
-- de entrega: ciudad, calle principal, número, calle secundaria y
-- referencia". La dirección de hoy vino del dBase toda tipeada en un solo
-- campo; acá se guarda SEGMENTADA, que es lo que Asinfo va a poder
-- importar por plantilla.
--
-- No toca `scintela.cliente` ni Asinfo: es lo que el cliente dijo, al lado
-- de lo que teníamos. La oficina lo mira y decide qué pasar a la ficha
-- (/clientes/datos-del-portal). `previo` guarda la foto de la ficha en ese
-- momento, para ver qué cambió.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.portal_datos_cliente (
    codigo_cli        varchar(20)  PRIMARY KEY,
    correo_facturas   varchar(120) NOT NULL,
    celular           varchar(20)  NOT NULL,
    telefono          varchar(20),
    ciudad            varchar(60)  NOT NULL,
    calle_principal   varchar(120) NOT NULL,
    numero            varchar(20)  NOT NULL,
    calle_secundaria  varchar(120) NOT NULL,
    referencia        varchar(200),
    previo            jsonb,
    cargado_en        timestamptz  NOT NULL DEFAULT now(),
    actualizado_en    timestamptz  NOT NULL DEFAULT now(),
    -- Cuándo la oficina pasó correo/teléfono a la ficha (NULL = todavía no).
    aplicado_en       timestamptz,
    aplicado_por      varchar(30)
);

COMMENT ON TABLE scintela.portal_datos_cliente IS
    'Lo que el cliente cargó en el portal (primer ingreso o Perfil): correo, celular, fijo y dirección de entrega segmentada. No es la ficha.';
