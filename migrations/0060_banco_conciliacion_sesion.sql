-- 0060_banco_conciliacion_sesion.sql
-- Sesión persistente de conciliación bancaria — Sprint 1 Reforma 2026-05-28.
--
-- TMT 2026-05-28 dueña: 'puede quedar abierta esa pagina hasta no cerrar la
-- conciliacion? (boton de terminar y guardar)'. Hoy la pantalla post-procesar
-- vive solo en memoria del request: cerrar el browser pierde el progreso.
-- Esta tabla guarda el extracto parseado + estado abierta/cerrada para que
-- la dueña pueda subir el xlsx, cerrar el browser, volver al rato y seguir.

CREATE TABLE IF NOT EXISTS scintela.banco_conciliacion_sesion (
    id               SERIAL PRIMARY KEY,
    no_banco         INTEGER NOT NULL,
    usuario          VARCHAR(50) NOT NULL,
    abierta_en       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cerrada_en       TIMESTAMP,
    cerrada_por      VARCHAR(50),
    extracto_hash    VARCHAR(64),                  -- sha256 del archivo subido
    extracto_nombre  VARCHAR(200),                 -- nombre original del archivo
    extracto_payload JSONB NOT NULL,               -- lista MovBanco serializada
    matches_hechos   INTEGER NOT NULL DEFAULT 0,
    pdf_path         TEXT
);

-- Solo puede haber UNA sesión abierta por (no_banco, usuario). Si quieren
-- abrir otra, primero hay que cerrar la actual.
CREATE UNIQUE INDEX IF NOT EXISTS banco_conciliacion_sesion_abierta_uniq
    ON scintela.banco_conciliacion_sesion (no_banco, usuario)
 WHERE cerrada_en IS NULL;

CREATE INDEX IF NOT EXISTS banco_conciliacion_sesion_no_banco_idx
    ON scintela.banco_conciliacion_sesion (no_banco, abierta_en DESC);
