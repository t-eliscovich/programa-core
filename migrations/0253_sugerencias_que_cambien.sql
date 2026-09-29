-- =====================================================================
-- 0253 · Las sugerencias del día se rearman con la regla que no repite
-- =====================================================================
-- Dueña 29/09/2026: las 5 de SEP del 29 eran las mismas del 28 (sin ventas
-- en el medio, la regla elegía lo mismo). Desde ahora un cliente que salió
-- no vuelve a salir por 3 días si hay otros (`sugerencias.DIAS_SIN_REPETIR`).
--
-- Las del 29/09 que ya se habían armado con la regla vieja y en las que nadie
-- apretó WhatsApp se borran para que se rearmen con la nueva la próxima vez
-- que el vendedor abra la app. Las que tienen clicks se quedan (son dato de
-- seguimiento). Idempotente y sólo toca ese día.
-- =====================================================================
DELETE FROM scintela.sugerencia_dia
 WHERE fecha = DATE '2026-09-29'
   AND whatsapp_veces = 0
   AND (vend, fecha) NOT IN (SELECT vend, fecha FROM scintela.sugerencia_dia
                              WHERE fecha = DATE '2026-09-29' AND whatsapp_veces > 0);
