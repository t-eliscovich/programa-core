-- =====================================================================
-- 0246 · El WhatsApp del vendedor, para la tarjeta del portal del cliente
-- =====================================================================
-- Dueña 09/09/2026: "poné el whatsapp de cada vendedor". El cliente ve en
-- su portal la tarjeta "Su vendedor en Intela" con nombre y nada más; el
-- canal con el vendedor es WhatsApp (04/09: sin el correo, es personal).
--
-- Vive en scintela.vendedor (el vendedor como figura comercial), no en
-- seguridad.usuario: un vendedor puede tener dos usuarios y el número es
-- uno. Se carga por la pantalla /portal-aviso. Asinfo tiene el teléfono en
-- `usuario.telefono` pero en 3 de 8 nada más, así que no se sincroniza:
-- se carga a mano.
-- =====================================================================

ALTER TABLE scintela.vendedor
    ADD COLUMN IF NOT EXISTS whatsapp VARCHAR(20);

COMMENT ON COLUMN scintela.vendedor.whatsapp IS
    'Celular del vendedor tal como se cargó (09… o 593…). El portal lo normaliza a wa.me; vacío = sin botón.';
