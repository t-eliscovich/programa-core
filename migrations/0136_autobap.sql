-- 0136 — Conversión AUTOMÁTICA anticipo USD → compra BAP al recibir la
-- importación, + la campanita de avisos (TMT 2026-07-29, dueña).
--
-- QUÉ AUTOMATIZA: hoy, cuando llega una importación, Asinfo marca la recepción
-- sola y un humano tilda los anticipos y aprieta "Convertir a compra". Ese
-- último paso es el único manual. Ahora lo hace el programa.
--
-- DECISIÓN DE LA DUEÑA (29/07): "convierte solo, entiendo que va a diferir del
-- dBase". La conversión mueve el monto de ANTICIPOS a COMPRAS **sólo en PC**
-- (la utilidad no cambia: los dos son activo), así que hasta que se haga el BAP
-- en el dBase las líneas del balance difieren. La campanita deja el recordatorio
-- con el código y el monto exactos. Ver feedback
-- `no-convertir-si-dbase-lo-tiene-en-anticipos` — esta regla la reemplaza para
-- las importaciones NUEVAS (de acá en adelante); los 4 pendientes viejos siguen
-- congelados por la fecha de corte.
--
-- NINGUNA de las dos tablas está en el TABLE_MAP de scripts/import_dbf.py →
-- sobreviven al sync del dBase (mismo patrón que importacion_pago y
-- dolares_anio).

CREATE TABLE IF NOT EXISTS scintela.autobap_config (
    id               SMALLINT PRIMARY KEY DEFAULT 1,
    activo           BOOLEAN        NOT NULL DEFAULT FALSE,
    -- Sólo se miran recepciones POSTERIORES a esta fecha. Se sella el día que
    -- se prende: evita que el primer barrido dispare todo el histórico.
    fecha_corte      DATE,
    -- Frenos por corrida. Si se superan NO se convierte nada y se avisa.
    tope_importaciones SMALLINT     NOT NULL DEFAULT 5,
    tope_usd         NUMERIC(14,2)  NOT NULL DEFAULT 60000,
    usuario_modifica VARCHAR(50),
    fecha_modifica   TIMESTAMPTZ    DEFAULT now(),
    CONSTRAINT autobap_config_una_sola_fila CHECK (id = 1)
);

INSERT INTO scintela.autobap_config (id, activo)
     VALUES (1, FALSE)
ON CONFLICT (id) DO NOTHING;

-- Avisos + auditoría. Una fila por evento (conversión hecha, o freno que la
-- detuvo). `leido` alimenta el contador de la campanita.
CREATE TABLE IF NOT EXISTS scintela.autobap_log (
    id_autobap_log   SERIAL PRIMARY KEY,
    tipo             VARCHAR(20)    NOT NULL,   -- conversion | freno | error
    im_numero        VARCHAR(30),
    codigo_prov      VARCHAR(5),
    ref_num          INTEGER,
    anio             SMALLINT,
    fecha_recepcion  DATE,
    kg               NUMERIC(14,2),
    n_anticipos      SMALLINT,
    importe          NUMERIC(14,2),
    id_compra        INTEGER,
    comprobante      VARCHAR(20),
    mensaje          VARCHAR(400),
    leido            BOOLEAN        NOT NULL DEFAULT FALSE,
    creado_en        TIMESTAMPTZ    DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_autobap_log_leido
    ON scintela.autobap_log (leido, creado_en DESC);

-- Una conversión por importación+corrida: el índice evita que dos procesos
-- (el hilo de fondo y el botón "Convertir ahora") logueen lo mismo dos veces.
CREATE INDEX IF NOT EXISTS ix_autobap_log_im
    ON scintela.autobap_log (im_numero, creado_en DESC);
