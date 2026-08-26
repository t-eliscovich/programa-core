-- 0229 · El aviso de MH 66-67 era un falso positivo: es poliéster
--
-- TMT 2026-08-26. La campanita avisó que a MH 66-67 le faltaba plata por
-- cargar (2,3138 US$/kg contra una "banda normal" de 2,7–3,4). Tamara se lo
-- preguntó a Andrés y la respuesta cierra el caso:
--
--   *"No falta nada por pasar. Ese hilo es de poliéster, que tiene un precio
--   menor al polialgodón. Por ejemplo un hilo de polialgodón está en este
--   momento a 2,75, mientras que uno de poliéster está a 1,70."*
--
-- O sea que la banda no era una banda: era el precio de UN tipo de hilo. La
-- alarma no encontró un error, encontró hilo más barato. La regla se cambia
-- aparte (pasa a comparar contra el promedio por TIPO DE HILADO); esto le da
-- vuelta el aviso que quedó dicho, con el mismo mecanismo que usan los vigías:
-- el MISMO renglón pasa a ✅ y vuelve a no leído.
--
-- El número 0227 se saltea a propósito: se lo llevó otra migración del mismo
-- día (el número es compartido) y el archivo que tenía ese número acá nunca
-- llegó a correr.

UPDATE scintela.aviso
   SET nivel   = 'ok',
       titulo  = 'MH 66-67 · listo, no faltaba plata: es hilo de poliéster',
       detalle = 'El poliéster va a 1,70 el kilo y el polialgodón a 2,75, y la '
                 || 'alarma los comparaba contra un solo promedio. Andrés '
                 || 'confirmó que no falta nada por pasar.',
       leido   = FALSE
 WHERE clave = 'import-sin-plata:IM-0000608+IM-0000609'
   AND nivel <> 'ok';

-- Que vuelva a verse en la campanita de todos, no sólo en la de quien no lo
-- había abierto todavía.
DELETE FROM scintela.aviso_leido
 WHERE id_aviso IN (
    SELECT id_aviso FROM scintela.aviso
     WHERE clave = 'import-sin-plata:IM-0000608+IM-0000609'
 );
