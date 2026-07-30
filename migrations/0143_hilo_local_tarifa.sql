-- 0143 — Tarifario $/kg de las COMPRAS LOCALES de hilo (TMT 2026-07-30, dueña).
--
-- POR QUÉ: la fábrica no sólo importa hilo, también le compra a proveedores
-- LOCALES (HY = Hiltexpoy, EP = El Peral). Esas compras viven en Asinfo como
-- `factura_proveedor` SIN fila en `factura_proveedor_importacion` (número
-- CMTR-… en vez de IM-…) y entran físicamente a la bodega 51, igual que una
-- importación. En volumen no son marginales: desde ago-2025 EP metió 203.825 kg
-- y HY 168.244 kg, del orden de la mitad del hilo importado.
--
-- No tienen ANTICIPO: llega la mercadería y nace el pasivo, igual que en
-- tejeduría. Por eso la carga es automática y el importe sale de un TARIFARIO,
-- no de Asinfo — decisión de la dueña 30/07: *"asinfo nunca nos importa en
-- plata. fijate en dbase cómo la cargan. tomá el tarifario con montos de
-- dbase"*.
--
-- `patron` = substring del CÓDIGO DE PRODUCTO de la recepción de Asinfo
-- (ej. '22/1-65:35CAR-10%-HY'). NULL = tarifa por defecto del proveedor.
-- El patrón más específico (el más largo que matchea) gana. Misma mecánica que
-- scintela.tejeduria_tarifa (mig 0133) — se repite la tabla en vez de compartir
-- porque el dominio es otro y las claves no colisionan.
--
-- ⚠ LA TARIFA VA **SIN IVA**. El dBase graba COMPRAS.IMPORTE **con IVA 15%**
--   (probado 3 veces: (a) IMPORTE/KG/1,15 da tarifas redondas en las 26
--   facturas de feb-jul 2026; (b) ALTAS.PRG L63/70 imprime la columna rotulada
--   "S/IVA" como II/KG/1.15; (c) el ND de pago cuadra al centavo con
--   base + IVA − ret.fuente 2% − ret.IVA 10%). El motor multiplica por 1,15 al
--   crear la compra para que PC quede igual que el dBase.
--
-- NO está en el TABLE_MAP de scripts/import_dbf.py → el sync del dBase no la
-- toca (es un parámetro que nace en PC; el dBase no lo tiene).
--
-- Tarifas sembradas: PROMEDIO PONDERADO POR KG (dueña: "el tarifario hace un
-- promedio") de los $/kg s/IVA del COMPRAS.DBF real, atribuidos a cada producto
-- cruzando el nº de factura del CONCEPTO contra el numero_factura de Asinfo.
-- `vistas` en la nota = los $/kg distintos que se promediaron.

CREATE TABLE IF NOT EXISTS scintela.hilo_local_tarifa (
    id_tarifa        SERIAL PRIMARY KEY,
    cod_prov         VARCHAR(5)     NOT NULL,
    patron           VARCHAR(60),
    tarifa           NUMERIC(12,4)  NOT NULL,
    nota             VARCHAR(120),
    usuario_modifica VARCHAR(50),
    fecha_modifica   TIMESTAMPTZ    DEFAULT now()
);

-- UNIQUE con patron NULL: en Postgres NULL != NULL, así que un UNIQUE simple no
-- impide dos defaults del mismo proveedor. Índice sobre COALESCE (igual 0133).
CREATE UNIQUE INDEX IF NOT EXISTS ux_hilo_local_tarifa_prov_patron
    ON scintela.hilo_local_tarifa (cod_prov, COALESCE(patron, ''));

INSERT INTO scintela.hilo_local_tarifa (cod_prov, patron, tarifa, nota, usuario_modifica)
SELECT * FROM (VALUES
    ('EP', '14/1-65:35-OP-PERAL',   2.9500, '10 facturas, todas a 2,95 (feb-abr 2026)', 'migracion-0143'),
    ('HY', '14/1-65:35-OP-hy',      2.5500, '4 facturas, todas a 2,55 (mar-abr 2026)',  'migracion-0143'),
    ('HY', '22/1-65:35CAR-10%-HY',  3.5249, 'promedio de 3 facturas (3,40 y 3,65)',     'migracion-0143'),
    ('HY', '24/1-65:35-CAR-HY10',   3.6163, 'promedio de 3 facturas (3,45 y 3,70)',     'migracion-0143'),
    ('HY', '100D/36F-POL-HENGYI',   2.2500, '1 factura (36736, abr 2026)',              'migracion-0143'),
    ('HY', '22/1-65:35CAR-2%-HY',   3.4000, '1 factura (36122, feb 2026)',              'migracion-0143'),
    ('HY', '16/1-65:35-OPEN-HY',    2.6000, '1 factura (36487, mar 2026)',              'migracion-0143'),
    ('HY', '20/1-65:35CAR-2%-HY',   3.6000, '1 factura (36925, may 2026)',              'migracion-0143'),
    ('HY', '24/1-65:35-CAR-HY 2%',  3.4500, '1 factura (36629, abr 2026)',              'migracion-0143'),
    ('HY', '14/1-65:35-CAR-HY',     2.9000, '1 factura (37649, jul 2026)',              'migracion-0143')
) AS v(cod_prov, patron, tarifa, nota, usuario_modifica)
WHERE NOT EXISTS (
    SELECT 1 FROM scintela.hilo_local_tarifa t
     WHERE t.cod_prov = v.cod_prov
       AND COALESCE(t.patron, '') = COALESCE(v.patron, '')
);
