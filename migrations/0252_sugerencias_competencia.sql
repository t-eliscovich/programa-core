-- =====================================================================
-- 0252 · Las 5 sugerencias del día para la competencia (Mi Cartera)
-- =====================================================================
-- Dueña 28/09/2026: *"veamos que vendio cada uno en estos dias y ofrezcamos
-- parecido para clientes que ya les vendieron… un anuncio 5 sugerencias para
-- la competencia. esta tela en estos colores a x persona. y ya manda el
-- mensaje por whatsapp"* · *"queremos sacar cantidad no puntos"* ·
-- *"agrega para trackear en uso de la app esto. y que cambie diario"*.
--
-- Una fila por sugerencia, por vendedor, por DÍA. Se arma la primera vez que
-- el vendedor (o la dueña en el preview) abre el Inicio ese día, y ya no se
-- toca hasta el día siguiente: así "las 5 de hoy" son las mismas cinco a las
-- 8 y a las 18, y se puede medir después qué pasó con cada una.
--
-- El seguimiento ("uso de la app") son las dos últimas columnas: cuántas
-- veces el vendedor apretó WhatsApp en esa sugerencia y cuándo fue la
-- primera. Lo que el cliente compró después NO se guarda acá: se cruza al
-- leer contra `parado_venta`, que es la fuente de lo vendido en la carrera.
--
-- Tabla nueva, no toca ningún dato existente.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.sugerencia_dia (
    fecha            date          NOT NULL,
    vend             varchar(10)   NOT NULL,
    orden            smallint      NOT NULL,
    codigo_cli       varchar(20)   NOT NULL,
    nombre           varchar(200),
    tela             varchar(120)  NOT NULL,
    -- [{"color": "NEG", "nombre": "Negro", "kg": 772.4}, ...] — los colores
    -- tal cual se mostraron ese día.
    colores          jsonb         NOT NULL,
    -- Lo que ese cliente suele llevar de esa tela, topeado por lo que hay.
    kg_posible       numeric(12,2) NOT NULL,
    -- 593XXXXXXXXX, o NULL si el cliente no tiene celular cargado.
    whatsapp         varchar(20),
    creado_en        timestamptz   NOT NULL DEFAULT now(),
    whatsapp_veces   integer       NOT NULL DEFAULT 0,
    whatsapp_primero timestamptz,
    PRIMARY KEY (fecha, vend, orden)
);

CREATE INDEX IF NOT EXISTS sugerencia_dia_fecha
    ON scintela.sugerencia_dia (fecha DESC);

COMMENT ON TABLE scintela.sugerencia_dia IS
    'Las 5 sugerencias diarias de la competencia en el Inicio de Mi Cartera, y cuántas veces se abrió WhatsApp en cada una.';
