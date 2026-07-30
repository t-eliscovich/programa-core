-- 0139 — NOVEDADES: una sola tabla de avisos para todo el programa.
--
-- TMT 2026-07-30 (dueña): *"la campanita debería funcionar también para cargas
-- de tejeduría, de fórmulas, no la hagas para compras. hacé notificaciones
-- globales"*. Hasta hoy la campanita leía `autobap_log`, que es el historial de
-- UN proceso (la conversión de importaciones) con sus columnas propias.
--
-- Esta tabla es al revés: no sabe de qué proceso viene, sólo guarda el aviso ya
-- redactado (título + detalle) más lo mínimo para ordenarlo y filtrarlo. Cada
-- proceso que quiera avisar llama a `modules.avisos.avisar(...)` y listo — no
-- hace falta una tabla ni una migración por proceso.
--
-- `clave` es el anti-repetido: un proceso de fondo corre cada N minutos y
-- reintenta lo mismo, así que cada aviso trae una clave única y estable
-- (p.ej. "tejeduria:compra:812") y el INSERT la ignora si ya está.

CREATE TABLE IF NOT EXISTS scintela.aviso (
    id_aviso    BIGSERIAL PRIMARY KEY,
    fuente      TEXT        NOT NULL,               -- tejeduria | importaciones | quimicos | …
    nivel       TEXT        NOT NULL DEFAULT 'ok',  -- ok (se cargó) | alerta | error
    titulo      TEXT        NOT NULL,
    detalle     TEXT,
    importe     NUMERIC(14, 2),
    cantidad    INTEGER,
    url         TEXT,                               -- a dónde ir a verlo
    clave       TEXT,                               -- anti-repetido (único)
    leido       BOOLEAN     NOT NULL DEFAULT FALSE,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS aviso_clave_uk
    ON scintela.aviso (clave) WHERE clave IS NOT NULL;

CREATE INDEX IF NOT EXISTS aviso_no_leidos_ix
    ON scintela.aviso (leido, creado_en DESC);

CREATE INDEX IF NOT EXISTS aviso_fuente_ix
    ON scintela.aviso (fuente, creado_en DESC);
