-- 0162 — Los KILOS en la captura del día: el resumen para accionistas.
--
-- TMT 2026-08-05 (dueña, viendo la primera versión de /informes/dia):
-- *"la explicación debería ser más: se produjo tanta tela, eso cambió de valor
-- de x a x. se vendió tanto etc. algo más senior resumido digestible para
-- accionistas"*.
--
-- El desglose por componente y por documento contesta "¿de dónde salió cada
-- peso?", que es la pregunta del que audita. El accionista hace otra: **qué
-- pasó en la fábrica**. Y esa se contesta en KILOS, no en cuentas contables.
--
-- Los kilos por etapa no se pueden reconstruir para atrás: salen de Asinfo en
-- vivo (saldo de bodega) o del cuadro del dBase del mes corriente. Si no
-- quedan guardados en la captura, mañana ya no hay forma de saber cuántos
-- kilos había hoy a las 07:00. Por eso van acá y no se calculan al leer.
--
-- La TARIFA ($/kg) va al lado de los kilos por la misma razón de siempre: el
-- stock se valúa a promedio ponderado, así que la tarifa mueve el valor de
-- TODO el stock de un saque. Sin las dos columnas no se puede decir si la tela
-- "valió más" porque se produjo o porque se revaluó — que para un accionista
-- son dos noticias completamente distintas.
--
-- Las ventas y las compras del día NO se guardan acá a propósito: se calculan
-- al leer, filtrando por `fecha` del documento (no por `fecha_crea`, que el
-- sync del dBase pisa). Así una factura anulada después deja de contarse, que
-- es lo correcto para un resumen que se vuelve a mirar.

ALTER TABLE scintela.dia_captura
    ADD COLUMN IF NOT EXISTS hilado_kg     NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS hilado_ukg    NUMERIC(12, 4),
    ADD COLUMN IF NOT EXISTS tejido_kg     NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS tejido_ukg    NUMERIC(12, 4),
    ADD COLUMN IF NOT EXISTS terminado_kg  NUMERIC(16, 2),
    ADD COLUMN IF NOT EXISTS terminado_ukg NUMERIC(12, 4);
