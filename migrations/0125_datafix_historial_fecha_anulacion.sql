-- 0125_datafix_historial_fecha_anulacion.sql
-- TMT 2026-07-16 (dueña): en el historial las ANULACIONES y EDICIONES de
-- posdat salían con fecha_operacion = fecha del posdatado (a veces futura,
-- ej. 31/07 = fecha del cheque), no la fecha en que se hizo la acción. Por
-- eso esas filas quedaban SIEMPRE arriba del historial.
--
-- El fix de código (modules/posdat/queries.py) ya carga estas acciones con
-- la fecha del día (today_ec). Esta datafix corrige las que YA estaban
-- cargadas: pone fecha_operacion = fecha_creacion (cuándo se hizo la acción).
--
-- Solo toca posdat_anulada / posdat_edit_importe donde difieren. Idempotente
-- (tras correr, fecha_operacion == fecha_creacion::date y el WHERE no matchea).
UPDATE scintela.mov_doble
   SET fecha_operacion = fecha_creacion::date
 WHERE tipo IN ('posdat_anulada', 'posdat_edit_importe')
   AND fecha_operacion <> fecha_creacion::date;
