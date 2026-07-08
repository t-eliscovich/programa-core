-- 0115_precios_seed_dbf_correcto.sql
-- TMT 2026-07-07: la tabla scintela.precios ya existía en producción con
-- valores viejos (0,25 más bajos que PRECIOS.DBF) — un seed previo. El seed
-- de 0114 usa ON CONFLICT DO NOTHING, así que NO los pisó. Esta migración
-- fuerza los valores CANÓNICOS de PRECIOS.DBF (2026-07-07) con UPSERT, para
-- que la "Lista de precios" muestre exactamente la matriz del dBase
-- (BLANCO JERSEY = 9,12, etc.). Idempotente y re-corrible.
--
-- Es el baseline del dBase, NO una edición de usuario — por eso sobrescribe.
-- Ediciones futuras por la UI (usuario_edita <> NULL) NO se re-corren solas,
-- así que esta migración de un solo tiro no vuelve a pisarlas.
DO $$
BEGIN
    IF to_regclass('scintela.precios') IS NOT NULL THEN
        INSERT INTO scintela.precios
            (clase, descripcio, jersey, pique, toper, alemania, rib, cuellos,
             lycra, falso, kiana, medical, micro, james)
        VALUES
            (1, 'BLANCO',     9.12,  9.23,  9.28,  8.35,  9.81, 12.43, 11.95,  9.44, 8.88, 12.02, 8.88, 8.88),
            (2, 'BAJOS',      9.84,  9.94,  9.40,  8.35, 10.55, 13.17, 12.73,  9.56, 8.88, 12.02, 8.88, 8.88),
            (3, 'MEDIOS',    10.57, 10.67,  9.81,  8.35, 11.32, 13.89, 12.73,  9.99, 8.88, 12.02, 8.88, 8.88),
            (4, 'JASPEADOS', 10.66, 10.77,  9.51,  8.35, 11.01, 13.63, 12.73, 10.41, 8.88, 12.02, 8.88, 8.88),
            (5, 'FUERTES',   11.29, 11.39, 10.48,  8.35, 12.09, 14.61, 13.15, 10.64, 8.88, 12.02, 8.88, 8.88)
        ON CONFLICT (clase) DO UPDATE SET
            descripcio = EXCLUDED.descripcio,
            jersey   = EXCLUDED.jersey,
            pique    = EXCLUDED.pique,
            toper    = EXCLUDED.toper,
            alemania = EXCLUDED.alemania,
            rib      = EXCLUDED.rib,
            cuellos  = EXCLUDED.cuellos,
            lycra    = EXCLUDED.lycra,
            falso    = EXCLUDED.falso,
            kiana    = EXCLUDED.kiana,
            medical  = EXCLUDED.medical,
            micro    = EXCLUDED.micro,
            james    = EXCLUDED.james;
    END IF;
END $$;
