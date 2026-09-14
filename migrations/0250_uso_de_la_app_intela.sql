-- =====================================================================
-- 0250 · «Uso de la app» suma a Intela como tercer mundo
-- =====================================================================
-- TMT 2026-09-14: *"hace que uso haya tabs, vendedores, clientes, intela.
-- hace un buen trabajo de medición"*.
--
-- No hay cambio de esquema: `scintela.uso_pantalla` (mig 0232) ya alcanza —
-- `usuario`/`vend` ya distinguen vendedor de oficina, y el índice
-- (usuario, ts DESC) ya sirve a los tres. Lo único que cambiaba era el
-- código: `modules/uso/registro.hay_que_registrar()` sólo anotaba a quien
-- tenía `vend` cargado (o al cliente del portal); ahora anota a cualquiera
-- logueado. Esta migración sólo deja el comentario de la tabla al día, para
-- que quien la lea en el futuro no crea que sigue siendo sólo vendedores.
-- =====================================================================

COMMENT ON TABLE scintela.uso_pantalla IS
    'Visitas (GET) de vendedores, oficina (Intela) y clientes del portal. Las escrituras van en scintela.bitacora_acciones.';
