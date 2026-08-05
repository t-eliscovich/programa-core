-- 0161 — LA EXPLICACIÓN DEL DÍA: por qué subió (o bajó) la utilidad.
--
-- TMT 2026-08-04 (dueña): *"quiero agregar un check diario, quiero que cada día
-- puedas darme una explicación de por qué subió la utilidad. comparás balance de
-- mañana y a fin de día"*. Y, sobre el residuo: *"no debería haber residuo.. y
-- tenemos que ir entrenando esto"*.
--
-- La grabadora (mig 0150, `scintela.traza_utilidad`) ya contesta QUÉ COMPONENTE
-- se movió: stock, cartera, pasivos. No contesta QUÉ DOCUMENTO lo movió, y ese
-- es el nivel en el que una explicación sirve para algo.
--
-- El problema para llegar a ese nivel es el sync del dBase: `import_dbf.py`
-- borra POSDAT, COMPRA, DOLARES, ACTIVOS y RETIROS enteras en cada corrida, así
-- que `fecha_crea` queda pisada con la hora del sync y NO se puede preguntar
-- "¿qué se cargó hoy?". La única forma que sobrevive a eso es guardar una FOTO
-- DE DETALLE propia y diffearla: lo que está en la foto nueva y no en la vieja
-- entró; lo que estaba y no está, salió. Eso es `dia_detalle`.
--
-- Las tres tablas:
--
--   dia_captura   — una fila por captura (07:00 y 19:00 de Ecuador). Guarda la
--                   utilidad y sus 11 componentes. Es AUTOSUFICIENTE a
--                   propósito: no depende de traza_utilidad, así el Δ del día
--                   se calcula con dos filas de esta misma tabla.
--   dia_detalle   — la foto RODANTE a nivel documento. Se reemplaza entera en
--                   cada captura. Es la única tabla del sistema que se pisa;
--                   lo que hay que conservar (el diff) ya salió a dia_movimiento.
--   dia_movimiento— qué entró, salió o cambió entre la captura anterior y ésta,
--                   con su aporte firmado a la utilidad. Es lo permanente.
--
-- El invariante que persigue todo esto:
--
--     Δ utilidad = Σ (aporte de cada movimiento)      ← sin residuo
--
-- Se cumple POR CONSTRUCCIÓN gracias a las filas sintéticas `#ajuste:<comp>`:
-- si el detalle por documento no llega a explicar el componente entero, la
-- diferencia se guarda como una fila más, con nombre y a la vista. Un
-- `#ajuste` no es un error de cuadratura — es la lista de tareas del
-- entrenamiento. El día que no aparezca ninguno, el sistema explica el 100%.

CREATE TABLE IF NOT EXISTS scintela.dia_captura (
    id_captura  BIGSERIAL   PRIMARY KEY,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fecha_ec    DATE        NOT NULL,          -- día de ECUADOR (UTC−5)
    momento     TEXT        NOT NULL,          -- manana | cierre | manual
    nota        TEXT,                          -- lo que la dueña anota a mano

    utilidad    NUMERIC(16, 2),
    patr_neto   NUMERIC(16, 2),

    -- Los 11 componentes, en el mismo orden del balance
    caja        NUMERIC(16, 2),
    bancos      NUMERIC(16, 2),
    cheques     NUMERIC(16, 2),
    facturas    NUMERIC(16, 2),
    antic       NUMERIC(16, 2),
    vsto        NUMERIC(16, 2),
    vqx         NUMERIC(16, 2),
    umaq        NUMERIC(16, 2),
    uact        NUMERIC(16, 2),
    totp        NUMERIC(16, 2),
    uret        NUMERIC(16, 2)
);

-- Una sola captura ancla por día y momento: el hilo de fondo pasa cada 2
-- minutos y tiene que entrar UNA vez. Las manuales no se limitan.
CREATE UNIQUE INDEX IF NOT EXISTS ux_dia_captura_ancla
    ON scintela.dia_captura (fecha_ec, momento)
    WHERE momento IN ('manana', 'cierre');

CREATE INDEX IF NOT EXISTS ix_dia_captura_fecha
    ON scintela.dia_captura (fecha_ec DESC, creado_en DESC);


-- La foto rodante. `cantidad` y `precio` van aparte del importe para el stock:
-- se valúa a promedio ponderado, así que la tarifa mueve el valor de TODO el
-- stock de un saque y hay que poder distinguir "entraron kilos" de "cambió el
-- $/kg" (misma razón que las columnas kg/ukg de traza_utilidad).
CREATE TABLE IF NOT EXISTS scintela.dia_detalle (
    componente  TEXT          NOT NULL,   -- caja|bancos|cheques|facturas|antic|vsto|vqx|umaq|uact|totp|uret
    doc_id      TEXT          NOT NULL,   -- id interno, o '#...' si es sintética
    etiqueta    TEXT,                     -- lo que la dueña reconoce (número + nombre)
    importe     NUMERIC(16, 2) NOT NULL,
    cantidad    NUMERIC(16, 2),           -- kg, cuando aplica
    precio      NUMERIC(12, 4),           -- $/kg, cuando aplica
    PRIMARY KEY (componente, doc_id)
);


CREATE TABLE IF NOT EXISTS scintela.dia_movimiento (
    id_mov      BIGSERIAL   PRIMARY KEY,
    id_captura  BIGINT      NOT NULL
                REFERENCES scintela.dia_captura (id_captura) ON DELETE CASCADE,
    componente  TEXT        NOT NULL,
    tipo        TEXT        NOT NULL,     -- alta | baja | cambio
    doc_id      TEXT,
    etiqueta    TEXT,
    importe_antes   NUMERIC(16, 2),
    importe_despues NUMERIC(16, 2),
    delta       NUMERIC(16, 2) NOT NULL,  -- Δ del componente
    aporte      NUMERIC(16, 2) NOT NULL,  -- Δ × signo (pasivo va al revés)
    regla       TEXT,                     -- el nombre en castellano de por qué
    familia     TEXT                      -- utilidad | traspaso | sin_explicar
);

CREATE INDEX IF NOT EXISTS ix_dia_movimiento_captura
    ON scintela.dia_movimiento (id_captura, componente);
