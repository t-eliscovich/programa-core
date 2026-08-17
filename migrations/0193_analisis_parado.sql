-- 0193_analisis_parado.sql
-- Sección "Análisis" — primera pantalla: LO PARADO y a quién llamar.
-- Tamara, 17/08/2026: "hacé como un link que nos lleve a todo un menú nuevo",
-- "por ahora yo y andrés".
--
-- ⭐ Por qué hay tablas y no se calcula al vuelo: el cálculo sale de Asinfo por
-- Metabase y tarda ~7 s (2 s los parados + 5 s los llamados). Eso es aceptable
-- para un botón "Actualizar", no para abrir una pantalla. Las tablas son la
-- caché; la pantalla lee Postgres y abre instantánea.
--
-- ⭐ Y `parado_cohorte` NO es caché: es el ÚNICO dato que no se puede recalcular.
-- Guarda cuándo se marcó cada tela × color y con cuántos kilos. Si se pierde,
-- se pierde la historia de qué se hizo con lo parado — pedido textual de la
-- dueña: "si empezamos a venderlas, que no se nos vayan de la lista". Por eso
-- vive en su propia tabla y el refresh sólo INSERTA claves nuevas: nunca borra
-- ni actualiza una fila existente.

-- ── La cohorte congelada ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scintela.parado_cohorte (
    subcategoria   VARCHAR(120)  NOT NULL,
    color          VARCHAR(10)   NOT NULL,
    fecha_marcado  DATE          NOT NULL,
    kg_al_marcar   NUMERIC(12,2) NOT NULL,
    PRIMARY KEY (subcategoria, color)
);

-- ── La foto de hoy (se pisa entera en cada actualización) ───────────────────
CREATE TABLE IF NOT EXISTS scintela.parado_foto (
    subcategoria   VARCHAR(120)  NOT NULL,
    color          VARCHAR(10)   NOT NULL,
    stock_kg       NUMERIC(12,2) NOT NULL DEFAULT 0,
    kg_vendidos    NUMERIC(12,2) NOT NULL DEFAULT 0,
    ultima_venta   DATE,
    clientes       INTEGER       NOT NULL DEFAULT 0,
    anio_pista     INTEGER,
    PRIMARY KEY (subcategoria, color)
);

-- ── A quién llamar, por TELA (el color no importa para la llamada) ──────────
CREATE TABLE IF NOT EXISTS scintela.parado_llamado (
    subcategoria   VARCHAR(120)  NOT NULL,
    codigo_cli     VARCHAR(10)   NOT NULL,
    nombre         VARCHAR(200),
    provincia      VARCHAR(80),
    vendedor       VARCHAR(80),
    vend_pc        VARCHAR(3),
    kg             NUMERIC(12,2) NOT NULL DEFAULT 0,
    ultima_compra  DATE,
    colores        INTEGER       NOT NULL DEFAULT 0,
    anio           INTEGER       NOT NULL,
    PRIMARY KEY (subcategoria, codigo_cli)
);

CREATE INDEX IF NOT EXISTS idx_parado_llamado_vend
    ON scintela.parado_llamado (vend_pc);

-- ── Cuándo se actualizó por última vez ──────────────────────────────────────
-- Una sola fila (id = 1). Sin esto la pantalla no puede decir de cuándo son los
-- números, y una lista de llamados sin fecha se usa igual — pero mal.
CREATE TABLE IF NOT EXISTS scintela.parado_refresh (
    id             INTEGER PRIMARY KEY,
    actualizado    TIMESTAMP,
    items          INTEGER,
    llamados       INTEGER,
    ok             BOOLEAN NOT NULL DEFAULT TRUE,
    detalle        TEXT,
    CONSTRAINT parado_refresh_una_fila CHECK (id = 1)
);

INSERT INTO scintela.parado_refresh (id, actualizado, items, llamados, ok, detalle)
VALUES (1, NULL, 0, 0, TRUE, 'nunca se actualizó')
ON CONFLICT (id) DO NOTHING;

-- ⚠ NO se siembra el permiso `analisis.ver` a propósito. Accionista y
-- Administrador tienen wildcard `*`, así que Tamara y Andrés entran sin más;
-- cualquier otro rol recibe 404 (`requiere_permiso` devuelve 404, no 403, para
-- que no se entere de que la sección existe). El día que haya que abrírsela a
-- alguien más, ahí sí va una migración que la agregue a su rol.
