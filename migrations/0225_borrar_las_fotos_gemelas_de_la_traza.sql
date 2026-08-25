-- Las fotos GEMELAS de la traza, y los movimientos que regrabaron.
--
-- 🚨 TMT 2026-08-25 (dueña), mirando /informes/traza: *"hay algo raro, la traza
-- no se está moviendo"*. No estaba parada: guardaba CADA foto DOS veces, con
-- dos a diez segundos de diferencia. El freno de cinco minutos era una variable
-- de proceso (`_ultimo_ts`) y desde el 24/08 hay DOS procesos sirviendo el
-- mismo código —el programa de la oficina y el portal del cliente—, cada uno
-- con su propio reloj. El freno se mudó a la base en el commit anterior; esto
-- limpia lo que quedó escrito.
--
-- Dos cosas quedaron sucias, y se limpian en este orden:
--
--  1. LOS MOVIMIENTOS REGRABADOS. La gemela lee el detalle ANTES de que la
--     primera lo aplique, así que calcula el MISMO diff y lo vuelve a grabar,
--     pero con Δ utilidad = 0. Eso es exactamente lo que la pantalla marcaba
--     como *"sin explicar"*: el residuo de cada una de esas siete ventanas era,
--     al centavo, la plata regrabada (la más grande, $ 13.911,28 a las 12:55
--     del 25/08). Medido antes de correr esto: 72 renglones en 11 ventanas del
--     24 y el 25/08.
--
--  2. LAS FOTOS QUE QUEDARON VACÍAS. Una gemela sin movimientos y con la MISMA
--     utilidad que su par no dice nada: sale con Δ 0 y las once columnas en
--     blanco, y era la mitad de los renglones de la pantalla. Medido: 98 ya
--     estaban vacías (desde el 07/08, los reinicios de cada deploy) y 9 quedan
--     vacías después del punto 1.
--
-- ⭐ Lo que NO se toca, a propósito:
--   · la gemela que tiene movimientos PROPIOS (54 fotos): son traspasos que
--     netean cero en la utilidad pero movieron documentos de verdad. Borrar la
--     foto los borraría con ella y nadie los volvería a calcular;
--   · la gemela con utilidad DISTINTA a su par (71 fotos): ahí el balance se
--     movió de verdad entre las dos lecturas. Borrar una correría esa plata a
--     la ventana siguiente, que es peor que un renglón de más;
--   · las anclas del día (`momento <> 'foto'`) y cualquier foto de la que
--     cuelgue una `dia_captura`. La utilidad y el balance no se tocan nunca:
--     salen de la foto del balance, no de estos renglones.


-- ── 1. Los movimientos que la gemela volvió a grabar ───────────────────────
WITH par AS (
    SELECT id_traza,
           lag(id_traza)  OVER (ORDER BY id_traza) AS id_par,
           creado_en - lag(creado_en) OVER (ORDER BY id_traza) AS separacion
      FROM scintela.traza_utilidad
)
DELETE FROM scintela.dia_movimiento m2
 USING par
 WHERE m2.id_traza = par.id_traza
   AND par.separacion <= INTERVAL '30 seconds'
   AND EXISTS (
       SELECT 1
         FROM scintela.dia_movimiento m1
        WHERE m1.id_traza   = par.id_par
          AND m1.componente = m2.componente
          AND m1.doc_id IS NOT DISTINCT FROM m2.doc_id
          AND m1.aporte     = m2.aporte
   );


-- ── 2. Las fotos gemelas que no dicen nada ─────────────────────────────────
WITH par AS (
    SELECT id_traza, utilidad, momento,
           lag(utilidad)  OVER (ORDER BY id_traza) AS utilidad_par,
           creado_en - lag(creado_en) OVER (ORDER BY id_traza) AS separacion
      FROM scintela.traza_utilidad
)
DELETE FROM scintela.traza_utilidad t
 USING par
 WHERE t.id_traza = par.id_traza
   AND par.separacion <= INTERVAL '30 seconds'
   AND par.utilidad IS NOT DISTINCT FROM par.utilidad_par
   AND par.momento = 'foto'
   AND NOT EXISTS (SELECT 1 FROM scintela.dia_movimiento m
                    WHERE m.id_traza = t.id_traza)
   AND NOT EXISTS (SELECT 1 FROM scintela.dia_captura c
                    WHERE c.id_traza = t.id_traza);
