-- 0173 — LA NOTA DEL DÍA, POR MAIL.
--
-- TMT 2026-08-06: *"la nota diaria por mail… hacelo con teliscovich@gmail.com"*.
--
-- La nota del cierre ya existe (`dia.mensaje_whatsapp`) pero Programa Core no
-- manda nada: el botón abre `wa.me/?text=` y el destinatario se elige en
-- WhatsApp. Ese bloqueo es REGULATORIO, no técnico — la Business API de Meta
-- exige un template aprobado, número verificado y un BSP con costo por
-- conversación. El mail no tiene nada de eso.
--
-- Dos tablas/columnas y nada más:
--
--   nota_destinatario — a quién le llega. Por PANTALLA, no por variable de
--                       entorno: sumar a un accionista no puede requerir un
--                       deploy.
--   dia_captura.nota_enviada_en — la idempotencia. 🚨 Va en la BASE y no en
--                       una variable de proceso: el hilo de fondo pasa cada
--                       dos minutos y el server reinicia, así que cualquier
--                       memoria en RAM manda el mail dos veces. Misma lección
--                       que el índice único de las capturas ancla (mig 0161).

CREATE TABLE IF NOT EXISTS scintela.nota_destinatario (
    id_destinatario BIGSERIAL   PRIMARY KEY,
    correo          TEXT        NOT NULL,
    nombre          TEXT,
    activo          BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usuario_crea    TEXT
);

-- Un correo una sola vez: dos filas iguales = dos mails idénticos.
CREATE UNIQUE INDEX IF NOT EXISTS ux_nota_destinatario_correo
    ON scintela.nota_destinatario (LOWER(TRIM(correo)));

INSERT INTO scintela.nota_destinatario (correo, nombre, usuario_crea)
VALUES ('teliscovich@gmail.com', 'Tamara', 'mig-0173')
ON CONFLICT DO NOTHING;

ALTER TABLE scintela.dia_captura
    ADD COLUMN IF NOT EXISTS nota_enviada_en TIMESTAMPTZ;
