-- 0133 — Tarifas $/kg de tejeduría tercerizada (TMT 2026-07-26, pedido dueña).
--
-- POR QUÉ: las compras tipo K de tejeduría faltan en PC A PROPÓSITO (los kg
-- salen de Asinfo, no se tipean). La pantalla /produccion-tejeduria-asinfo ya
-- reconcilia producido vs cargado, pero el importe lo tenía que tipear un
-- humano porque el sistema no conocía la tarifa → las compras se cargaban
-- tarde o nunca (al 26/07 faltaban $4.970,49 de Reyes y $3.661,74 de Ponce,
-- la mitad del hueco de pasivos del compare vs dBase).
--
-- Ahora las tarifas viven acá, editables desde la pantalla por Accionistas y
-- Andrés (permiso 'tarifas.editar', que sólo satisfacen los roles wildcard '*'
-- — mismo criterio que 'precios.editar' de la mig 0114).
--
-- `patron` = substring del PRODUCTO de la OF de Asinfo (match ILIKE '%patron%').
-- NULL = tarifa por defecto del proveedor. El patrón más específico gana.
--
-- NO está en el TABLE_MAP de scripts/import_dbf.py → el sync del dBase no la
-- toca (es un parámetro que nace en PC, el dBase no lo tiene).
--
-- Tarifas sembradas: sacadas del COMPRAS.DBF real (tipo K, kg>0, importe>0),
-- verificadas contra Asinfo con coincidencias exactas al centavo:
--   RY (Reyes)  2,0125  → 22 de 34 facturas y TODAS las últimas desde el 03/07.
--   AP + HUF    1,0350  → OFT-000038545 "KW20 HUF40 R D/C LY" 320,50 kg
--                          = factura 532 del dBase, $331,72 exacto.
--   AP resto    0,5750  → OFT-000036779 + OFT-000037923 "KW20 R/N"
--                          617,85 + 2.410,40 = 3.028,25 kg = factura 531,
--                          $1.741,24 exacto.
--   UN          1,1500  → 6 de 6 facturas (sin actividad desde 16/06).
-- INTELA/KK NO va acá: su $/kg es una FÓRMULA (hilo_ukg + 0,50), no una tarifa.

CREATE TABLE IF NOT EXISTS scintela.tejeduria_tarifa (
    id_tarifa        SERIAL PRIMARY KEY,
    cod_prov         VARCHAR(5)     NOT NULL,
    patron           VARCHAR(60),
    tarifa           NUMERIC(12,4)  NOT NULL,
    nota             VARCHAR(120),
    usuario_modifica VARCHAR(50),
    fecha_modifica   TIMESTAMPTZ    DEFAULT now()
);

-- UNIQUE con patron NULL: en Postgres NULL != NULL, así que un UNIQUE simple
-- no impide dos defaults del mismo proveedor. Índice sobre COALESCE.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tejeduria_tarifa_prov_patron
    ON scintela.tejeduria_tarifa (cod_prov, COALESCE(patron, ''));

INSERT INTO scintela.tejeduria_tarifa (cod_prov, patron, tarifa, nota, usuario_modifica)
SELECT * FROM (VALUES
    ('RY', NULL,   2.0125, 'Reyes — tarifa única (22/34 facturas y todas desde 03/07)', 'migracion-0133'),
    ('AP', 'HUF',  1.0350, 'Ponce — artículos con HUF (verif. OFT-000038545 = factura 532)', 'migracion-0133'),
    ('AP', NULL,   0.5750, 'Ponce — resto (verif. KW20 R/N 3.028,25 kg = factura 531)', 'migracion-0133'),
    ('UN', NULL,   1.1500, 'Sin actividad desde 16/06 — 6/6 facturas a 1,15', 'migracion-0133')
) AS v(cod_prov, patron, tarifa, nota, usuario_modifica)
WHERE NOT EXISTS (
    SELECT 1 FROM scintela.tejeduria_tarifa t
     WHERE t.cod_prov = v.cod_prov
       AND COALESCE(t.patron, '') = COALESCE(v.patron, '')
);
