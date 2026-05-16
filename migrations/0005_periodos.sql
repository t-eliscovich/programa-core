-- =====================================================================
-- 0005_periodos
-- =====================================================================
-- Control de períodos contables. Cada mes (o período custom) puede
-- cerrarse; una vez cerrado, NO se permiten escrituras con fecha
-- anterior al corte (se valida a nivel app, no en triggers).
--
-- Estado:
--   'A' = abierto  (default)
--   'C' = cerrado
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.periodos_contables (
    id_periodo   SERIAL PRIMARY KEY,
    anio         INTEGER NOT NULL,
    mes          INTEGER NOT NULL CHECK (mes BETWEEN 1 AND 12),
    fecha_desde  DATE    NOT NULL,
    fecha_hasta  DATE    NOT NULL,
    estado       CHAR(1) NOT NULL DEFAULT 'A' CHECK (estado IN ('A','C')),
    cerrado_por  VARCHAR(40),
    fecha_cierre TIMESTAMP,
    motivo       VARCHAR(200),
    UNIQUE (anio, mes)
);

CREATE INDEX IF NOT EXISTS idx_periodos_estado
    ON scintela.periodos_contables (estado, fecha_hasta DESC);

COMMENT ON TABLE scintela.periodos_contables IS
    'Períodos contables mensuales. Al cerrar un período, las escrituras '
    'con fecha <= fecha_hasta quedan bloqueadas (control en capa app).';

-- Seed: genera los períodos mensuales desde 2020 hasta el actual (+6 meses).
DO $$
DECLARE
    y INTEGER;
    m INTEGER;
    anio_fin INTEGER := EXTRACT(YEAR FROM CURRENT_DATE)::int;
    mes_fin  INTEGER := EXTRACT(MONTH FROM CURRENT_DATE)::int;
BEGIN
    FOR y IN 2020..(anio_fin + 1) LOOP
        FOR m IN 1..12 LOOP
            EXIT WHEN y = anio_fin + 1 AND m > (mes_fin + 6 - 12);
            INSERT INTO scintela.periodos_contables
                (anio, mes, fecha_desde, fecha_hasta, estado)
            VALUES (
                y, m,
                make_date(y, m, 1),
                (make_date(y, m, 1) + INTERVAL '1 month - 1 day')::date,
                'A'
            )
            ON CONFLICT (anio, mes) DO NOTHING;
        END LOOP;
    END LOOP;
END$$;
