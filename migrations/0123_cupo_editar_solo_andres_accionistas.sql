-- Migration 0123: el cupo de clientes solo lo editan Andrés (Administrador) y
-- los Accionistas.
--
-- CONTEXTO (dueña 2026-07-09): "solo andres y accionistas pueden editar cupo de
-- clientes". El permiso `cupos.editar` ya existía en config/roles.py pero lo
-- tenían también Cobranzas, Ventas y QC (y no se enforce­aba en el código).
-- Ahora el código gatea la edición del cupo con `cupos.editar`; Accionista y
-- Administrador lo tienen por wildcard '*'. Sacamos el permiso de los otros 3.
DELETE FROM seguridad.permiso
 WHERE nombre_opcion = 'cupos.editar'
   AND id_rol IN (SELECT id_rol FROM seguridad.rol
                   WHERE nombre_rol IN ('Cobranzas', 'Ventas', 'QC'));
