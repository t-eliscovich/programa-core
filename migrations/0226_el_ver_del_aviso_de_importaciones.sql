-- 0226 · El "ver →" del aviso de importaciones, ya filtrado
--
-- TMT 2026-08-26 (dueña, sobre el aviso de MH 66-67 en la campanita): *"acá el
-- ver no me lleva a filtrado por esas importaciones"*. El aviso guardaba
-- `url = '/importaciones'` a secas, así que abría la lista entera y el caso
-- quedaba perdido entre las demás.
--
-- El código ya arma el link filtrado (`/importaciones?anio=todos&q=…`), pero un
-- aviso se escribe UNA sola vez —la `clave` lo hace idempotente— así que los
-- que ya están en la campanita nunca se reescriben solos. Esto les arregla el
-- link a los que quedaron.
--
-- Cómo se saca el `q` de lo que ya está escrito:
--   · alarma de US$/kg — el título arranca con el código del grupo
--     ("MH 66-67 · llegó hace 30 días…"). Sólo se toca si eso tiene forma de
--     código; si no, se deja el link a la lista entera, que es honesto;
--   · factura repartida — el título lleva VARIOS códigos ("MH 68, MH 69, MH
--     70") y `q` es un Y entre palabras, así que buscarlos juntos daría cero
--     filas. Va el número de factura del proveedor, que el detalle dice y que
--     está adentro de la nota de las tres.
--
-- `anio=todos` porque la pantalla muestra por defecto sólo el año en curso.

-- La factura repartida PRIMERO: su título también empieza con algo que puede
-- parecer un código, y así no se lo lleva la otra regla.
UPDATE scintela.aviso
   SET url = '/importaciones?anio=todos&q='
             || replace(substring(detalle from 'La factura (.+?) del proveedor'),
                        ' ', '+')
 WHERE fuente = 'importaciones'
   AND url = '/importaciones'
   AND substring(detalle from 'La factura (.+?) del proveedor') IS NOT NULL;

UPDATE scintela.aviso
   SET url = '/importaciones?anio=todos&q='
             || replace(split_part(titulo, ' · ', 1), ' ', '+')
 WHERE fuente = 'importaciones'
   AND url = '/importaciones'
   AND split_part(titulo, ' · ', 1) ~ '^[A-Z]{2,3} [0-9]+(-[0-9]+)?$';
