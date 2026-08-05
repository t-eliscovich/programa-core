-- Migración 0163: la meta de venta se mide en KILOS, no en dólares.
--
-- TMT 2026-08-05 (dueña, sobre el portal de vendedores): *"En las ventas de
-- mes por favor poner los kilos, las metas se mide en kilos (esto arreglas en
-- la app y en metas que ponemos en el programa)"*.
--
-- La fábrica se maneja en kg: el vendedor negocia precio por kilo, la
-- producción se planifica en kilos y la meta que la dueña le pone a cada uno
-- es de kilos vendidos. Medirla en dólares mezclaba dos noticias distintas —
-- vendió más tela, o vendió la misma tela más cara.
--
-- La columna se RENOMBRA (`monto` → `kg`) en vez de agregar una al lado. Una
-- tabla con `monto` y `kg` a la vez es una invitación a que la mitad del
-- código lea la equivocada, y el nombre viejo no dejaría rastro de que el
-- significado cambió: se leería como un monto, para siempre.
--
-- Y se BORRAN las metas viejas: la única cargada era RMY 2026-08 = 10.000
-- (dólares). Convertirla a kilos dividiendo por un precio promedio inventaría
-- un número que nadie decidió — a $9,6/kg darían 1.041 kg, y esa cifra no
-- sale de ninguna conversación. La dueña las vuelve a cargar en kg por
-- /comisiones/metas. Sin fila = sin meta, y sin meta el portal muestra el
-- facturado a secas, sin anillo (ver 0154).
--
-- ⚠ Sólo se borran las filas ANTERIORES a hoy: el deploy del código va antes
-- que la migración (el deploy NO corre migraciones), así que entre las dos
-- cosas la dueña ya puede estar cargando kilos. Un DELETE pelado se los
-- llevaría puestos.
--
-- Idempotente.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'scintela'
                  AND table_name   = 'vendedor_meta'
                  AND column_name  = 'monto')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'scintela'
                          AND table_name   = 'vendedor_meta'
                          AND column_name  = 'kg')
    THEN
        DELETE FROM scintela.vendedor_meta
         WHERE COALESCE(fecha_actualiza, '1900-01-01'::timestamp)
               < '2026-08-05'::timestamp;

        ALTER TABLE scintela.vendedor_meta RENAME COLUMN monto TO kg;
    END IF;
END $$;

COMMENT ON TABLE scintela.vendedor_meta IS
    'Meta de venta por vendedor y mes, en KILOS facturados (mig 0163). La '
    'anual se suma y la semanal se prorratea por días hábiles — no se cargan '
    'aparte. Sin fila = sin meta: el portal muestra el facturado sin anillo.';

COMMENT ON COLUMN scintela.vendedor_meta.kg IS
    'KILOS, no dólares. Se llamó `monto` hasta la mig 0163.';
