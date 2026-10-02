-- 0258 · Ajuste al cierre de un mes ya cerrado + segundo PDF del cierre
--
-- Tamara 02/10/2026. Asinfo corrigió el saldo de las bodegas a las 13:50:
-- se fueron 51.775 kg de hilo y 7.139 kg de tela cruda que figuraban en
-- stock desde julio (salidas que no habían bajado el saldo). La utilidad de
-- octubre bajó 191.504 de golpe, pero esos kilos ya no estaban al 30/09:
-- *"vamos a absorberlo en septiembre aunque haya cosas de julio"*,
-- *"poné en historia un ajuste así no afecta octubre"*.
--
-- `ajuste_cierre`: cada ajuste que se le hace a la foto de cierre de un mes
-- (scintela.historia). Guarda las líneas (etapa, kg, $/kg), el importe y la
-- foto ANTES/DESPUÉS, para poder deshacerlo y para volver a aplicarlo si la
-- foto de cierre se regraba (crear_snapshot_historia con forzar borra la fila
-- y la vuelve a insertar).
--
-- `cierre_paquete.version`: el PDF del cierre original queda como versión 1;
-- el que se arma con el ajuste es la versión 2 (y así). Nunca se pisa el
-- original.

CREATE TABLE IF NOT EXISTS scintela.ajuste_cierre (
    id_ajuste     serial PRIMARY KEY,
    anio          integer       NOT NULL,
    mes           integer       NOT NULL CHECK (mes BETWEEN 1 AND 12),
    id_historia   integer,
    motivo        text          NOT NULL,
    importe       numeric(14,2) NOT NULL,
    kg            numeric(14,2) NOT NULL DEFAULT 0,
    lineas        jsonb         NOT NULL DEFAULT '[]'::jsonb,
    antes         jsonb,
    despues       jsonb,
    creado_en     timestamptz   NOT NULL DEFAULT now(),
    creado_por    varchar(50),
    anulado_en    timestamptz,
    anulado_por   varchar(50)
);

CREATE INDEX IF NOT EXISTS ix_ajuste_cierre_mes
    ON scintela.ajuste_cierre (anio, mes) WHERE anulado_en IS NULL;

ALTER TABLE scintela.cierre_paquete
    ADD COLUMN IF NOT EXISTS version smallint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS nota    text;

ALTER TABLE scintela.cierre_paquete
    DROP CONSTRAINT IF EXISTS cierre_paquete_anio_mes_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_cierre_paquete_anio_mes_version
    ON scintela.cierre_paquete (anio, mes, version);
