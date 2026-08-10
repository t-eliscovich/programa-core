-- 0185 — la foto guarda el $/kg de tejido y de terminado.
--
-- 🚨 TMT 2026-08-10: *"¿se puede? porque Asinfo saca un poco más tarde el
-- stock real que la factura"*. Tenía razón: medido sobre las 2.320 ventanas
-- con detalle, sólo 68 de 137 tienen la venta Y la salida de stock en la misma
-- ventana, y agrupar por día tampoco lo salva (2 de 4 días). Así que el margen
-- NO puede salir de parear las dos patas.
--
-- Sale de la factura: kg vendidos × $/kg de terminado, calculable en el mismo
-- instante de la venta. El precio existe en el balance (`stock_etapas`) cuando
-- se saca la foto, pero hasta hoy sólo se persistía el del hilado — por eso el
-- valor de tejido y terminado "no se podía reconstruir".
--
-- Forward-only: las fotos ya guardadas se quedan sin el dato.
ALTER TABLE scintela.traza_utilidad
    ADD COLUMN IF NOT EXISTS tejido_ukg    numeric,
    ADD COLUMN IF NOT EXISTS terminado_ukg numeric;
