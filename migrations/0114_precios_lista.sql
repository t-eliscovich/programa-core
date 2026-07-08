-- 0114_precios_lista.sql
-- TMT 2026-07-07 (dueña): "Lista de precios" en Datos base — réplica de
-- PRECIOS.DBF. Matriz 5 clases de color (filas) x 12 tipos de tela (columnas),
-- valores = precio USD/kg. Ver todos; editar sólo Accionista/Administrador
-- (gate por permiso 'precios.editar', que los roles wildcard '*' satisfacen).
--
-- La tabla NO está en el TABLE_MAP del sync dBase -> las ediciones en PC
-- PERSISTEN (no las pisa el sync). El seed de abajo son los valores actuales
-- leídos de PRECIOS.DBF (2026-07-07). Un futuro re-import de PRECIOS.DBF se
-- enchufaría con un UPSERT por `clase` sobre esta misma tabla.
-- Idempotente y re-corrible.
DO $$
BEGIN
    IF to_regclass('scintela.precios') IS NULL THEN
        CREATE TABLE scintela.precios (
            clase       INTEGER       PRIMARY KEY,
            descripcio  VARCHAR(40)   NOT NULL,
            jersey      NUMERIC(10,2),
            pique       NUMERIC(10,2),
            toper       NUMERIC(10,2),
            alemania    NUMERIC(10,2),
            rib         NUMERIC(10,2),
            cuellos     NUMERIC(10,2),
            lycra       NUMERIC(10,2),
            falso       NUMERIC(10,2),
            kiana       NUMERIC(10,2),
            medical     NUMERIC(10,2),
            micro       NUMERIC(10,2),
            james       NUMERIC(10,2),
            actualizado TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
            usuario_edita VARCHAR(50)
        );
    END IF;

    -- Seed / refresh valores actuales de PRECIOS.DBF (2026-07-07).
    -- ON CONFLICT DO NOTHING: si la tabla ya tiene datos (ya se edito en PC),
    -- NO los pisa. El seed solo llena la primera vez.
    INSERT INTO scintela.precios
        (clase, descripcio, jersey, pique, toper, alemania, rib, cuellos,
         lycra, falso, kiana, medical, micro, james)
    VALUES
        (1, 'BLANCO',     9.12,  9.23,  9.28,  8.35,  9.81, 12.43, 11.95,  9.44, 8.88, 12.02, 8.88, 8.88),
        (2, 'BAJOS',      9.84,  9.94,  9.40,  8.35, 10.55, 13.17, 12.73,  9.56, 8.88, 12.02, 8.88, 8.88),
        (3, 'MEDIOS',    10.57, 10.67,  9.81,  8.35, 11.32, 13.89, 12.73,  9.99, 8.88, 12.02, 8.88, 8.88),
        (4, 'JASPEADOS', 10.66, 10.77,  9.51,  8.35, 11.01, 13.63, 12.73, 10.41, 8.88, 12.02, 8.88, 8.88),
        (5, 'FUERTES',   11.29, 11.39, 10.48,  8.35, 12.09, 14.61, 13.15, 10.64, 8.88, 12.02, 8.88, 8.88)
    ON CONFLICT (clase) DO NOTHING;
END $$;
