-- =====================================================================
-- 0239 · La hora a la que se emitió la factura
-- =====================================================================
-- TMT 2026-09-02 (dueña, mirando /facturas/183296): *"podés agregarles hora
-- y minutos de emitida. no segundos"*.
--
-- `factura.fecha` es un DATE: el dBase nunca supo la hora. Asinfo sí:
-- `factura_cliente.fecha_creacion` está en hora de Ecuador (ver
-- modules/asinfo/despacho_sin_factura.py — el reloj del servidor está en
-- UTC pero esa columna no). Se guarda acá, sin segundos, para que la ficha
-- no tenga que cruzar el puente cada vez que se abre. La llena
-- `modules/asinfo/hora_emision.py`: la auto-carga para las del día y la
-- ficha, una sola vez, para las que ya estaban.
--
-- NULL = todavía no se preguntó (o Asinfo no la conoce: las viejas del
-- dBase no tienen hora y nunca la van a tener).
ALTER TABLE scintela.factura
    ADD COLUMN IF NOT EXISTS hora_emision time;

COMMENT ON COLUMN scintela.factura.hora_emision IS
    'Hora (EC, sin segundos) a la que Asinfo emitió el documento. NULL = sin dato.';
