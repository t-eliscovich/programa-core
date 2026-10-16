-- 0265 · Borrador de «Producir» en /pedidos (Tamara 16/10/2026)
--
-- "Para cuando hice la mitad y quiero seguir más tarde, un botón de guardar
-- y que se mantenga hasta que mande". Un renglón por usuario y color (clave
-- = código + acabado): lo que escribió en Producir rollos / rib. Se borra
-- cuando ese color se manda a producción o cuando el usuario cancela.

CREATE TABLE IF NOT EXISTS scintela.pedidos_producir_borrador (
    usuario     TEXT        NOT NULL,
    clave       TEXT        NOT NULL,
    rollos      NUMERIC     NOT NULL DEFAULT 0,
    rib_kg      NUMERIC     NOT NULL DEFAULT 0,
    actualizado TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usuario, clave)
);
