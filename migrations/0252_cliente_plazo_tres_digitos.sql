-- 0252 — `cliente.pago` (plazo de pago en días) pasa de varchar(2) a varchar(3).
--
-- TMT 2026-09-24 (Andrés): en la ficha del cliente el "Plazo de pago (días)"
-- sólo dejaba poner 2 dígitos (tope 99 días); hay clientes con plazos de 120.
-- La columna venía del dBase con dos caracteres. Sólo ensancha: no cambia
-- ningún valor (los 'C' de contado y demás siguen como están).
ALTER TABLE scintela.cliente
    ALTER COLUMN pago TYPE varchar(3);
