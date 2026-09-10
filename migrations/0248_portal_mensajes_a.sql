-- =====================================================================
-- 0248 · A quién le llega por mail lo que el cliente escribe en el portal
-- =====================================================================
-- Dueña 10/09/2026: el cuadro de "Perfil" es un buzón de texto libre ("no
-- me gusta el botón" también entra). Además de la campanita/Novedades de la
-- oficina, el mensaje sale por MAIL a esta lista (separada por comas), que
-- se edita en /portal-aviso. Nace con la dueña.
-- =====================================================================

INSERT INTO scintela.nota_config (clave, valor)
VALUES ('portal_mensajes_a', 'teliscovich@gmail.com')
ON CONFLICT (clave) DO NOTHING;
