-- 0010_recientes.sql
--
-- "Últimos tocados" por usuario — UI: bloque "Recientes" arriba del sidebar
-- con los últimos 5 registros que el usuario abrió (cliente, factura, cheque,
-- proveedor, etc.). Mejora cualitativa grande para el día-a-día: nadie
-- memoriza URLs con ID, todos vuelven a los mismos 3-5 clientes calientes.
--
-- Invariantes:
--   - (id_usuario, tipo, id_ref) es PK: un mismo registro no aparece dos veces
--     por usuario — UPSERT bump de `tocado_en` lo re-sube al tope.
--   - El índice por `tocado_en DESC` cubre el listado "últimos 5" con una
--     sola página del btree.
--   - Trim: cuando se sobrepasan 50 registros por (usuario, tipo), se borran
--     los más viejos. Esto vive en queries.registrar(), no en trigger —
--     preferimos código visible a magia en DB.

CREATE TABLE IF NOT EXISTS seguridad.usuario_recientes (
    id_usuario int NOT NULL REFERENCES seguridad.usuario(id_usuario) ON DELETE CASCADE,
    tipo varchar(20) NOT NULL,   -- 'cliente' | 'factura' | 'cheque' | 'proveedor'
    id_ref varchar(40) NOT NULL, -- codigo_cli / numf / id_cheque / codigo_prov
    etiqueta varchar(200),       -- nombre legible para mostrar ("JTX — Jiménez SA")
    tocado_en timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id_usuario, tipo, id_ref)
);

CREATE INDEX IF NOT EXISTS idx_usuario_recientes_ts
    ON seguridad.usuario_recientes (id_usuario, tocado_en DESC);
