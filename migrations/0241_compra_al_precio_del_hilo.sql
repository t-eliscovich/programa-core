-- =====================================================================
-- 0241 · Una compra de hilo que entra al $/kg del hilado SIN importación
-- =====================================================================
-- TMT 2026-09-03 (dueña): pasó a compra un anticipo de MH del 2024 y la
-- utilidad de septiembre bajó 21.253. El $/kg del hilado sólo se mueve
-- con lo que Asinfo marca RECIBIDO en el mes (importaciones cruzadas por
-- cuenta + número) y con las compras locales; una compra de hilo que no
-- cruza con nada queda afuera del promedio ponderado: la plata sale del
-- anticipo y no entra a ningún lado.
-- *"pasalo como una compra sin cruzar o algo así y cambia el precio del
-- hilo"* → esta marca. Con ella, `mov_hilado_valuacion` suma el importe
-- (y los kg, si tiene) de la compra al promedio ponderado del mes de su
-- fecha: el stock de hilado se revalúa y la utilidad vuelve. Se marca y
-- desmarca desde la ficha de la compra.
ALTER TABLE scintela.compra
    ADD COLUMN IF NOT EXISTS al_precio_hilo boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN scintela.compra.al_precio_hilo IS
    'TRUE = esta compra de hilo (tipo H) entra al $/kg del hilado del mes aunque no cruce con una importación recibida en Asinfo.';
