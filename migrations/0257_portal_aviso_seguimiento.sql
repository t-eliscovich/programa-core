-- 0257 · Quién lee el aviso del portal
--
-- Tamara 02/10/2026: *"después hay que ver quién lee los mails, ¿podemos
-- hacer eso?"* → sí. Cada aviso que sale lleva una marca propia (`token`):
--   * una imagen invisible de portal.intela.com.ec/a/<token>.gif — cuando el
--     cliente abre el mail y su correo descarga las imágenes, queda
--     `abierto_en` (aproximado: Apple Mail la baja sola, Outlook/Hotmail a
--     veces las bloquean);
--   * el botón pasa por portal.intela.com.ec/a/<token> antes de ir al
--     portal — eso deja `clic_en`, que es el dato confiable.
-- Los avisos de antes de esta migración no tienen marca: salen como "—".

ALTER TABLE scintela.portal_aviso
    ADD COLUMN IF NOT EXISTS token      varchar(40),
    ADD COLUMN IF NOT EXISTS abierto_en timestamptz,
    ADD COLUMN IF NOT EXISTS aperturas  integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS clic_en    timestamptz,
    ADD COLUMN IF NOT EXISTS clics      integer NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS portal_aviso_token
    ON scintela.portal_aviso (token) WHERE token IS NOT NULL;
