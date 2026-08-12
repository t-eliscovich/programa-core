-- Migration 0187: lo que le falta a proforma_cabecera/detalle para poder
-- GUARDAR una proforma y volver a abrirla.
--
-- CONTEXTO (Alex por WhatsApp 2026-08-11, aprobado por la dueña el mismo día):
-- "podemos dejar la opción de guardar las proformas, ya que en ocasiones los
-- clientes piden aumentar o quitar algo y lo que ahora toca es repetir la
-- proforma en cada petición".
--
-- Las tablas ya existían (mig 0119) y queries.crear() ya estaba escrita, pero
-- NINGUNA ruta la llamaba: la pantalla calculaba e imprimía 100% en el browser.
-- Estas columnas son las que la pantalla tiene y la tabla no; sin ellas, una
-- Factura Proforma reabierta vuelve como cotización, sin flete y con los kilos
-- ya convertidos (perdiendo las piezas que se tipearon).
--
-- 🚨 `proforma_detalle.precio_unitario` se guarda CON IVA. La proforma es el
-- papel que ve el CLIENTE y habla el mismo idioma que la hoja de precios que
-- se le entrega (dueña 2026-08-05: "con IVA siempre"). En scintela.precios el
-- precio está NETO — son escalas DISTINTAS. Nadie divida esto por 1,15.

ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS flete NUMERIC(14,2) NOT NULL DEFAULT 0;
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS es_pedido BOOLEAN NOT NULL DEFAULT FALSE;

-- De qué proforma viene esta versión. A propósito SIN foreign key: las de más
-- de 7 días se borran (decisión de la dueña 2026-08-11), así que el padre puede
-- no existir. La pantalla muestra el número como TEXTO cuando ya no está, en
-- vez de un link roto.
ALTER TABLE scintela.proforma_cabecera
    ADD COLUMN IF NOT EXISTS id_origen INTEGER;

-- `tela` = la columna de scintela.precios (jersey/pique/…), que es lo que la
-- pantalla necesita para rearmar la fila; `nombre_producto` guarda la ETIQUETA
-- que se imprime y no siempre coincide (PUÑOS cobra por la columna `cuellos`).
ALTER TABLE scintela.proforma_detalle
    ADD COLUMN IF NOT EXISTS tela VARCHAR(20);
-- La CANTIDAD tal como se tipeó (piezas, cuellos, puños) antes de convertirse
-- a kilos. Sin esto, al reabrir una cotización se pierde en cuántas piezas se
-- había pensado y sólo quedan los kilos.
ALTER TABLE scintela.proforma_detalle
    ADD COLUMN IF NOT EXISTS cantidad NUMERIC(12,2);

-- La lista de recientes ordena por lo último creado.
CREATE INDEX IF NOT EXISTS idx_proforma_cab_creado
    ON scintela.proforma_cabecera(creado_en DESC);
