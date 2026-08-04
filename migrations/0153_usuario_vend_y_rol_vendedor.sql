-- Migración 0153: atar un USUARIO a un código de VENDEDOR + rol "Vendedor".
--
-- TMT 2026-08-03 (dueña): "Necesitamos crear varios usuarios nuevos, que
-- serían los vendedores. Ya tenemos los códigos de vendedores, habría que
-- mapear código con el nombre y qué clientes tienen. […] tienen que tener
-- acceso solo a sus clientes, sus facturas, sus cheques."
--
-- El mapeo vendedor→clientes YA existe: `scintela.cliente.vend` +
-- `scintela.vendedor` (migraciones 0032 / 0034 / 0120, los 6 oficiales son
-- PPR, EDG, SEP, JQU, FL1, RMY). Lo único que faltaba era atar la PERSONA
-- que se loguea a ese código — hasta hoy `seguridad.usuario` no tenía ningún
-- campo de scope de datos, y el modelo de permisos gatea la PANTALLA, no la
-- FILA.
--
-- Esta migración:
--   1) agrega `seguridad.usuario.vend` (varchar 3, nullable);
--   2) crea el rol "Vendedor" con el permiso `micartera.ver`.
--
-- NO crea ningún usuario ni asigna ningún código: eso se hace por la
-- pantalla /usuarios (regla de oro — todo cambio en producción se hace por
-- las pantallas reales).
--
-- `vend` NULL / '' = usuario normal, sin scope. La app trata "tiene vend" y
-- no "se llama Vendedor" como la condición de acotado, para que renombrar el
-- rol no abra el acceso en silencio (ya pasó con Dueño→Accionista y la IP
-- allowlist).
--
-- Idempotente.

ALTER TABLE seguridad.usuario
    ADD COLUMN IF NOT EXISTS vend VARCHAR(3);

COMMENT ON COLUMN seguridad.usuario.vend IS
    'Código de vendedor (scintela.vendedor.codigo). Si está cargado, el '
    'usuario sólo ve los clientes con cliente.vend = este código y sólo '
    'puede entrar a /mi-cartera. NULL = usuario normal, sin acotar.';

-- A propósito SIN foreign key a scintela.vendedor: `seguridad` y `scintela`
-- son esquemas distintos y el catálogo de vendedores lo edita la dueña por
-- pantalla (puede desactivar uno). Un FK acá convertiría un cambio de
-- catálogo en un error de escritura. La integridad se valida en la UI de
-- /usuarios, que ofrece un desplegable con los vendedores activos.
CREATE INDEX IF NOT EXISTS ix_usuario_vend
    ON seguridad.usuario (vend)
    WHERE vend IS NOT NULL;

-- Rol "Vendedor" -----------------------------------------------------------
-- Sólo `micartera.ver`. NO lleva clientes.ver / facturas.ver / cheques.ver:
-- esos permisos abren la pantalla COMPLETA (todos los clientes de la
-- empresa). Ver config/roles.py.
INSERT INTO seguridad.rol (nombre_rol)
SELECT 'Vendedor'
 WHERE NOT EXISTS (
     SELECT 1 FROM seguridad.rol WHERE lower(nombre_rol) = 'vendedor'
 );

INSERT INTO seguridad.permiso (id_rol, nombre_opcion)
SELECT r.id_rol, 'micartera.ver'
  FROM seguridad.rol r
 WHERE lower(r.nombre_rol) = 'vendedor'
   AND NOT EXISTS (
       SELECT 1 FROM seguridad.permiso p
        WHERE p.id_rol = r.id_rol AND p.nombre_opcion = 'micartera.ver'
   );
