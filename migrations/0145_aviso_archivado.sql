-- 0145 — Poder ARCHIVAR un aviso de Novedades.
--
-- TMT 2026-07-30 (dueña, mostrando la campanita: *"sigo teniendo esto como
-- notificación"*). Los dos avisos de «Tejedor sin reconocer: M RTEYES / M
-- RERYES» se emitieron a las 21:27, ANTES de que el programa aprendiera a
-- leer el apellido mal tipeado (deploy 22:04). El problema ya está resuelto —
-- las dos OFs son de Reyes y sus compras están apareadas — pero el aviso
-- quedó, porque hasta ahora un aviso sólo se podía marcar LEÍDO, nunca sacar.
--
-- "Leído" y "ya no aplica" son dos cosas distintas y hacían falta las dos:
-- leído = lo vi; archivado = esto ya no es una novedad, no me lo muestres más.
-- Sin esto, cada aviso que el propio arreglo deja obsoleto se queda para
-- siempre en la lista y le va sacando valor a la pantalla entera.
--
-- No se BORRA la fila: archivar es reversible (?archivados=1 los muestra) y el
-- historial de lo que el programa vio es justamente el punto de Novedades.

ALTER TABLE scintela.aviso
    ADD COLUMN IF NOT EXISTS archivado    boolean NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS archivado_en timestamptz,
    ADD COLUMN IF NOT EXISTS archivado_por varchar(60);

-- La lista filtra por `NOT archivado` en cada carga de la pantalla.
CREATE INDEX IF NOT EXISTS ix_aviso_archivado
    ON scintela.aviso (archivado, creado_en DESC);
