-- =====================================================================
-- 0232 · Qué mira cada vendedor adentro de la app
-- =====================================================================
-- TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
-- aplicación? ¿y qué movimientos hace?"*.
--
-- Hoy la segunda mitad ya estaba y la primera no. `scintela.bitacora_acciones`
-- guarda sólo las ESCRITURAS (POST/PUT/DELETE/PATCH, ver `auth._should_audit`),
-- y un vendedor casi no escribe: mira su cartera, abre la ficha de un cliente,
-- imprime un estado de cuenta, manda un PDF por WhatsApp. Todo eso son GET y no
-- dejaban rastro. Los ingresos tampoco: `/login` está en la lista de rutas que
-- la bitácora saltea.
--
-- Esta tabla guarda las VISITAS. Los movimientos siguen viviendo en la
-- bitácora — la pantalla de uso muestra las dos cosas juntas y no duplica nada.
--
-- ⭐ Se registra SÓLO a los usuarios que tienen `vend` cargado (los 6
-- vendedores). Es lo que se preguntó, y es el volumen que se justifica: ~6
-- personas por ~100 pantallas al día. La oficina no se registra; para abrirlo,
-- una sola condición en `modules/uso/registro.py`.
--
-- ⚠ El preview de la dueña (`/mi-cartera?vend=PPR` con permiso wildcard) NO se
-- registra, porque quien lo abre no tiene `vend` propio. Los números del
-- vendedor son del vendedor.
--
-- Es una tabla nueva y un INSERT best-effort: no toca un solo dato de los que
-- ya existen.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.uso_pantalla (
    id_uso       bigserial    PRIMARY KEY,
    ts           timestamptz  NOT NULL DEFAULT now(),
    -- Quién. `usuario` es el username de seguridad.usuario; `vend` es su
    -- código de vendedor, copiado acá para que la pantalla no dependa de que
    -- el usuario siga existiendo (ni de que le sigan dejando el mismo vend).
    usuario      varchar(40)  NOT NULL,
    vend         varchar(10),
    -- Qué abrió. `pantalla` guarda el ENDPOINT de Flask (mi_cartera.cliente),
    -- no el título: los títulos cambian y el nombre lindo se resuelve al leer,
    -- así un cambio de texto no deja media tabla con el nombre viejo.
    ruta         varchar(200) NOT NULL,
    pantalla     varchar(60),
    -- El cliente que estaba mirando, cuando la ruta lo lleva. Es la columna
    -- que contesta "¿a cuántos de sus clientes les abrió la ficha?".
    codigo_cli   varchar(20),
    -- 'celular' | 'computadora'. La dueña ya sabía que trabajan del teléfono
    -- ("vendedores casi siempre usan celular", 03/08); esto lo muestra.
    dispositivo  varchar(12),
    ip           varchar(45)
);

-- La pantalla de un vendedor: sus visitas, de la más nueva a la más vieja.
CREATE INDEX IF NOT EXISTS uso_pantalla_usuario_fecha
    ON scintela.uso_pantalla (usuario, ts DESC);

-- El resumen de todos en un rango de fechas.
CREATE INDEX IF NOT EXISTS uso_pantalla_fecha
    ON scintela.uso_pantalla (ts DESC);

COMMENT ON TABLE scintela.uso_pantalla IS
    'Visitas (GET) de los usuarios vendedores. Las escrituras van en scintela.bitacora_acciones.';
