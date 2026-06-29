-- 0108_importacion_pago_por_im.sql
-- TMT 2026-06-29 (dueña): el estado de recepción/deuda/pago debe identificarse
-- por la IMPORTACIÓN FÍSICA (número IM- de Asinfo, único), NO por (proveedor,
-- número de la Nota). Motivo: el número de la Nota (ej. "AC 40") se REUSA con
-- los años — hay una importación AC 40 de 2023 (IM-0000227) y otra de 2026
-- (IM-0000593). Con la clave vieja (prov,nº) ambas caían en la misma fila →
-- recibir una marcaba la otra y los kg/deuda se duplicaban.
--
-- PC-only (extiende scintela.importacion_pago). Idempotente.
DO $$
BEGIN
  IF to_regclass('scintela.importacion_pago') IS NULL THEN
    RAISE EXCEPTION 'Falta la migración 0104 (scintela.importacion_pago).';
  END IF;
END $$;

ALTER TABLE scintela.importacion_pago
  ADD COLUMN IF NOT EXISTS im_numero VARCHAR(20);

-- La clave vieja (prov,nº) NO es única en el tiempo → la sacamos.
DROP INDEX IF EXISTS scintela.ux_importacion_pago_prov_num;

-- Limpiar filas inertes sin im_numero (restos de pruebas / contabilizar viejo,
-- todo en cero) para que no choquen con las nuevas filas por importación.
DELETE FROM scintela.importacion_pago
 WHERE im_numero IS NULL
   AND recibido_pc = FALSE AND pagada = FALSE
   AND contabilizada = FALSE AND COALESCE(monto_pagado, 0) = 0;

-- Nueva identidad: una fila por importación física.
CREATE UNIQUE INDEX IF NOT EXISTS ux_importacion_pago_im
  ON scintela.importacion_pago (im_numero) WHERE im_numero IS NOT NULL;
