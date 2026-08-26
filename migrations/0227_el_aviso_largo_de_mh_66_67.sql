-- 0227 · El aviso de MH 66-67, corto
--
-- TMT 2026-08-26, después de acortar los textos: *"esto sigue acá feo largo"*.
-- Y tenía razón: el commit anterior cambió cómo se ESCRIBEN los avisos nuevos,
-- pero un aviso se escribe UNA sola vez —la `clave` lo hace idempotente— así
-- que el que ya estaba en la campanita se quedó con el párrafo viejo. Y no va a
-- volver a entrar: la alarma sólo dispara entre los 30 y los 31 días de
-- recibida, y ésta ya los pasó.
--
-- Los números no se recalculan, se copian del propio aviso (kg, dólares
-- cargados, US$/kg y faltante son los que decía el texto largo; los verifiqué
-- contra /importaciones?anio=todos&q=MH+66-67, que muestra las dos partidas en
-- una fila: 47.730 kg y US$ 110.439,62). Lo único que cambia es cómo se lee, y
-- los separadores pasan a los de Ecuador.
--
-- Es un UPDATE de UNA fila, atado a la clave del grupo Y al título viejo: si
-- alguien ya lo tocó o el aviso no es ése, no hace nada.

UPDATE scintela.aviso
   SET titulo  = 'MH 66-67 · 2,31 el kilo, y suele ser 2,70. ¿Faltan compras por cargar?',
       detalle = '47.730 kg con US$ 110.439,62 cargados: faltarían unos '
                 || 'US$ 18.431. Llegó hace 30 días.'
 WHERE clave = 'import-sin-plata:IM-0000608+IM-0000609'
   AND titulo LIKE 'MH 66-67 · llegó hace %';
