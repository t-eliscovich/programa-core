-- =====================================================================
-- 0245 · El cliente avisa un pago desde su portal
-- =====================================================================
-- Dueña 04/09/2026 ("dale con todas"). Lo primero que le abre la puerta al
-- cliente a ESCRIBIR — y aun así no escribe plata: deja un AVISO con el
-- comprobante (foto o PDF) y alguien de la oficina lo carga por la pantalla
-- de siempre (Cobranza). La tabla es ese buzón.
--
-- El comprobante va adentro de la fila (bytea, tope 5 MB en la pantalla):
-- no hay carpeta de archivos en el server que sobreviva un deploy, y son
-- pocos y chicos.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.portal_aviso_pago (
    id_aviso_pago    bigserial    PRIMARY KEY,
    codigo_cli       varchar(20)  NOT NULL,
    -- 'cheque' | 'transferencia' | 'deposito' | 'efectivo'
    tipo             varchar(20)  NOT NULL,
    importe          numeric(14,2),
    fecha            date,
    referencia       varchar(60),
    nota             varchar(400),
    archivo          bytea,
    archivo_nombre   varchar(120),
    archivo_tipo     varchar(60),
    creado_en        timestamptz  NOT NULL DEFAULT now(),
    -- Alguien de la oficina lo cargó (o lo descartó) y lo marcó.
    atendido_en      timestamptz,
    atendido_por     varchar(40),
    atendido_nota    varchar(200)
);

CREATE INDEX IF NOT EXISTS portal_aviso_pago_pendientes
    ON scintela.portal_aviso_pago (creado_en DESC) WHERE atendido_en IS NULL;

CREATE INDEX IF NOT EXISTS portal_aviso_pago_cliente
    ON scintela.portal_aviso_pago (UPPER(TRIM(codigo_cli)), creado_en DESC);

-- El texto de "Cómo pagar" que ve el cliente. Lo edita la dueña desde
-- /portal-aviso; vacío = la pantalla dice que llame a la oficina.
INSERT INTO scintela.nota_config (clave, valor)
VALUES ('portal_como_pagar', '')
ON CONFLICT (clave) DO NOTHING;

COMMENT ON TABLE scintela.portal_aviso_pago IS
    'Avisos de pago que dejan los clientes desde su portal, con el comprobante. La oficina los carga por Cobranza.';
