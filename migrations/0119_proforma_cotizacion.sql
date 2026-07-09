-- Migration 0119: proforma_cabecera / proforma_detalle (COTIZACIONES).
--
-- CONTEXTO (dueña 2026-07-09 "recrear Proformas en Ingresos, como el dBase"):
-- FACTURAR.PRG del dBase arma una nota/factura moviéndose campo a campo con
-- ENTER: TIP (tela) → color → KG → PRECIO (auto de PRECIOS.DBF, editable) →
-- IMPORTE = KG*PRECIO; al final descuento por volumen y por contado en cascada.
--
-- ACÁ es SOLO para COTIZAR: "no se va a facturar con esto pero es para cotizar"
-- (dueña). Por eso estas tablas NO tocan stock, facturas, posdat ni utilidad —
-- son una cotización que se guarda, se lista y se imprime. El precio de venta
-- sale de scintela.precios (matriz clase×tela, réplica de PRECIOS.DBF); la
-- clase de color (1..5 = BLANCO/BAJOS/MEDIOS/JASPEADOS/FUERTES) reemplaza el
-- viejo lookup por código de COSTOS.DBF (que no vive en Programa Core).
--
-- El módulo de solo-lectura (modules/proformas: lista + detalle) ya leía estas
-- tablas pero NINGUNA migración las creaba. Esta las crea de forma idempotente
-- (CREATE TABLE IF NOT EXISTS + ALTER ADD COLUMN IF NOT EXISTS por si ya
-- existían parciales). Re-corrible sin efecto.

CREATE TABLE IF NOT EXISTS scintela.proforma_cabecera (
    id_proforma                   SERIAL       PRIMARY KEY,
    id_cliente                    INTEGER      REFERENCES scintela.cliente(id_cliente),
    fecha_emision                 DATE         NOT NULL DEFAULT CURRENT_DATE,
    subtotal                      NUMERIC(14,2) NOT NULL DEFAULT 0,
    porcentaje_descuento_volumen  NUMERIC(6,2) NOT NULL DEFAULT 0,
    monto_descuento_volumen       NUMERIC(14,2) NOT NULL DEFAULT 0,
    subtotal_con_descuento        NUMERIC(14,2) NOT NULL DEFAULT 0,
    aplica_descuento_contado      BOOLEAN      NOT NULL DEFAULT FALSE,
    monto_descuento_contado       NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_final                   NUMERIC(14,2) NOT NULL DEFAULT 0,
    observaciones                 TEXT,
    usuario_crea                  VARCHAR(50),
    creado_en                     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scintela.proforma_detalle (
    id_detalle               SERIAL        PRIMARY KEY,
    id_proforma              INTEGER       NOT NULL
                                 REFERENCES scintela.proforma_cabecera(id_proforma)
                                 ON DELETE CASCADE,
    id_subcategoria_producto INTEGER,
    nombre_producto          VARCHAR(60),
    color                    VARCHAR(60),
    clase                    INTEGER,
    cantidad_kilos           NUMERIC(12,2) NOT NULL DEFAULT 0,
    precio_unitario          NUMERIC(12,4) NOT NULL DEFAULT 0,
    precio_total             NUMERIC(14,2) NOT NULL DEFAULT 0
);

-- Guards idempotentes por si las tablas ya existían con menos columnas.
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS porcentaje_descuento_volumen NUMERIC(6,2) NOT NULL DEFAULT 0;
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS aplica_descuento_contado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS observaciones TEXT;
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS usuario_crea VARCHAR(50);
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE scintela.proforma_detalle
    ADD COLUMN IF NOT EXISTS clase INTEGER;
ALTER TABLE scintela.proforma_detalle
    ADD COLUMN IF NOT EXISTS id_subcategoria_producto INTEGER;

CREATE INDEX IF NOT EXISTS idx_proforma_detalle_prof
    ON scintela.proforma_detalle(id_proforma);
CREATE INDEX IF NOT EXISTS idx_proforma_cab_cliente
    ON scintela.proforma_cabecera(id_cliente);
CREATE INDEX IF NOT EXISTS idx_proforma_cab_fecha
    ON scintela.proforma_cabecera(fecha_emision);
