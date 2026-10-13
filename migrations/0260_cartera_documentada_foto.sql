-- 0260 · Foto diaria de la CARTERA DOCUMENTADA por vendedor (13/10/2026)
--
-- Pedido de Andrés para la reunión de vendedores: "cuánto de la cartera de
-- cada vendedor está documentada en cheques". Tamara: sólo el %, y de varios
-- meses. La pantalla Estados de cuenta › Vendedor lo sabe sólo para HOY —
-- nadie guardaba la foto. Desde acá la foto diaria (health/all) graba una
-- fila por vendedor y por día; el cierre de mes es la del último día.
CREATE TABLE IF NOT EXISTS scintela.cartera_documentada_foto (
    fecha      DATE          NOT NULL,
    vend       VARCHAR(10)   NOT NULL,   -- '~' = (sin vendedor) = Intela
    vendedor   VARCHAR(80)   NOT NULL,
    clientes   INTEGER       NOT NULL DEFAULT 0,
    saldo      NUMERIC(14,2) NOT NULL DEFAULT 0,   -- "Saldo facturas" (neto)
    cheques    NUMERIC(14,2) NOT NULL DEFAULT 0,   -- "Cheques por cobrar"
    creado_en  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (fecha, vend)
);
