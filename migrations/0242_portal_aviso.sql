-- =====================================================================
-- 0242 · El aviso a los clientes: "su estado de cuenta está en el portal"
-- =====================================================================
-- Fase 4 del plan del portal (docs/notas-de-sesion/PLAN_PORTAL_CLIENTE_
-- 2026_08_24.md). Iba por WhatsApp; el alta en Meta no llegó y la dueña
-- decidió el 04/09/2026 arrancar POR MAIL: *"fase 4 hagámosla por mail por el
-- momento"*. Y: *"hasta no testear no mandamos nada a los clientes"* — por eso
-- el envío a clientes nace APAGADO (la clave de abajo en '0').
--
-- Esta tabla es la bitácora de cada aviso que salió: a quién, a qué correo,
-- cuándo, quién lo mandó y qué contestó SES. Es lo que deja leer "a este
-- cliente ya le avisamos el lunes pasado" y "a estos no les llegó".
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.portal_aviso (
    id_portal_aviso  bigserial    PRIMARY KEY,
    codigo_cli       varchar(20)  NOT NULL,
    correo           varchar(200) NOT NULL,
    -- 'prueba' cuando fue a la casilla de alguien de la casa para ver cómo
    -- sale; 'cliente' cuando fue de verdad.
    tipo             varchar(10)  NOT NULL DEFAULT 'cliente',
    ok               boolean      NOT NULL DEFAULT false,
    motivo           varchar(200),
    id_ses           varchar(120),
    enviado_por      varchar(40),
    enviado_en       timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS portal_aviso_cliente_fecha
    ON scintela.portal_aviso (UPPER(TRIM(codigo_cli)), enviado_en DESC);

CREATE INDEX IF NOT EXISTS portal_aviso_fecha
    ON scintela.portal_aviso (enviado_en DESC);

-- El interruptor. '0' = sólo pruebas a la casa; '1' = también a clientes.
INSERT INTO scintela.nota_config (clave, valor)
VALUES ('portal_aviso_a_clientes', '0')
ON CONFLICT (clave) DO NOTHING;

COMMENT ON TABLE scintela.portal_aviso IS
    'Cada aviso "su estado de cuenta está en el portal" que salió por mail, y qué pasó.';
