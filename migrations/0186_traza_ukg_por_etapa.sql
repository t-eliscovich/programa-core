-- 0186 — la foto guarda el $/kg de tejido y de terminado.
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
-- 🚨 Nació como 0185 y CHOCÓ con otra 0185 pusheada el mismo día desde otra
-- sesión (`0185_backfill_fechaout_nacidos_afuera.py`): el runner marca la
-- versión "0185" como aplicada UNA vez, así que ésta quedó registrada sin
-- correr. La foto empezó a insertar columnas que no existían y la grabadora
-- —que es fail-soft— dejó de guardar en silencio (última foto 15:43 EC).
-- ⭐ El número de migración es un recurso COMPARTIDO: antes de numerar, mirar
-- qué hay en origin/main.
--
-- Forward-only: las fotos ya guardadas se quedan sin el dato.
ALTER TABLE scintela.traza_utilidad
    ADD COLUMN IF NOT EXISTS tejido_ukg    numeric,
    ADD COLUMN IF NOT EXISTS terminado_ukg numeric;
