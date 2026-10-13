-- 0261 · La campanita del vendedor y "Avisar un problema" (13/10/2026)
--
-- Tamara: *"me gustaría una campanita y avisar al vendedor si algún cliente
-- pasa a rojo"* y *"algo para que puedan quejarse de algo de la app"* (le
-- llega a ella).
--
-- 1) semaforo_foto — el color del semáforo de cobranza de CADA cliente, una
--    fila por día. El semáforo se calculaba en el momento y no quedaba nada:
--    sin la foto de ayer no se puede saber quién "pasó" a rojo hoy.
-- 2) vendedor_aviso — lo que le llega a la campanita de cada vendedor
--    (cliente que pasó a rojo, respuesta a su aviso de problema…). `clave`
--    UNIQUE: el mismo aviso no se crea dos veces aunque la foto corra varias.
-- 3) reporte_app — los avisos de problemas de la app, con la foto opcional y
--    la respuesta de Tamara.
-- 4) Permiso `app.reportes` SIN ningún rol: la bandeja la ven sólo los
--    usuarios con `*` (Tamara pidió que le llegue a ella).

CREATE TABLE IF NOT EXISTS scintela.semaforo_foto (
    fecha       DATE         NOT NULL,
    codigo_cli  VARCHAR(10)  NOT NULL,
    color       VARCHAR(8)   NOT NULL,   -- rojo · amar · verde · inc
    motivos     TEXT,
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (fecha, codigo_cli)
);

CREATE TABLE IF NOT EXISTS scintela.vendedor_aviso (
    id_aviso    SERIAL       PRIMARY KEY,
    vend        VARCHAR(3)   NOT NULL,
    tipo        VARCHAR(20)  NOT NULL,   -- rojo · respuesta · jonathan
    titulo      VARCHAR(200) NOT NULL,
    detalle     TEXT,
    url         VARCHAR(300),
    clave       VARCHAR(120) NOT NULL UNIQUE,
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    visto_en    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS vendedor_aviso_vend_idx
    ON scintela.vendedor_aviso (vend, creado_en DESC);

CREATE TABLE IF NOT EXISTS scintela.reporte_app (
    id_reporte     SERIAL        PRIMARY KEY,
    usuario        VARCHAR(60)   NOT NULL,
    vend           VARCHAR(3),
    texto          TEXT          NOT NULL,
    pantalla       VARCHAR(300),
    dispositivo    VARCHAR(40),
    user_agent     VARCHAR(400),
    foto           BYTEA,
    foto_tipo      VARCHAR(40),
    creado_en      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    respuesta      TEXT,
    respondido_en  TIMESTAMPTZ,
    respondido_por VARCHAR(60),
    resuelto       BOOLEAN       NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS reporte_app_usuario_idx
    ON scintela.reporte_app (usuario, creado_en DESC);
