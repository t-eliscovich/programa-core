-- 0144 — El tarifario de hilo local va con UNA tarifa por PROVEEDOR.
--
-- TMT 2026-07-30 (dueña, corrigiendo la 0143): *"TARIFAS TE DIJE UN PROMEDIO.
-- NO 50 POR PROVEEDOR. UNO SOLO POR PROVEEDOR Y HACE UN PROMEDIO PONDERADO"*.
--
-- La 0143 sembró una fila por (proveedor, producto) — 10 filas — copiando el
-- mecanismo de patrones de tejeduría. Es más precisión de la que el tarifario
-- necesita y convierte una pantalla de consulta en una tabla de mantenimiento:
-- cada producto nuevo de Asinfo pediría una tarifa antes de poder cargar nada.
--
-- Queda UNA fila por proveedor (patron NULL = default), con el promedio
-- PONDERADO POR KG de todas sus facturas del COMPRAS.DBF (importe sin IVA
-- total ÷ kg total), que es la definición correcta del promedio de un $/kg:
--
--   HY (Hiltexpoy) → 275.216,99 s/IVA ÷ 89.623,57 kg = 3,0708  (16 facturas)
--   EP (El Peral)  → 148.735,75 s/IVA ÷ 50.418,80 kg = 2,9500  (10 facturas)
--
-- El mecanismo de `patron` NO se borra de la tabla: si algún día un producto
-- se sale mucho del promedio, se agrega una fila con su patrón y gana sobre el
-- default. Simplemente hoy no se usa, y la pantalla ya no lo pide.

-- Fuera las 10 filas por producto que sembró la 0143 (sólo esas: si alguien ya
-- cargó una tarifa a mano, se respeta).
DELETE FROM scintela.hilo_local_tarifa
 WHERE usuario_modifica = 'migracion-0143'
   AND patron IS NOT NULL;

INSERT INTO scintela.hilo_local_tarifa (cod_prov, patron, tarifa, nota, usuario_modifica)
SELECT * FROM (VALUES
    ('HY', NULL, 3.0708, 'Promedio ponderado por kg de 16 facturas (feb-jul 2026)', 'migracion-0144'),
    ('EP', NULL, 2.9500, 'Promedio ponderado por kg de 10 facturas (feb-abr 2026)', 'migracion-0144')
) AS v(cod_prov, patron, tarifa, nota, usuario_modifica)
WHERE NOT EXISTS (
    SELECT 1 FROM scintela.hilo_local_tarifa t
     WHERE t.cod_prov = v.cod_prov
       AND COALESCE(t.patron, '') = ''
);
