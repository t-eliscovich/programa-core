-- 0181 — El reverso del totalizar le dice a la traza CUÁLES facturas tocó.
--
-- TMT 2026-08-07, mirando /informes/traza: *"¿por qué una factura de MLZ me
-- quedó en otro renglón?"*. Las 13 facturas que el reverso restauró salían en
-- TRES renglones: el mov_doble sólo nombra la primera y la última
-- (origen_id/destino_id), así que las del medio no matcheaban ningún evento y
-- la traza las clasificaba por la FORMA del cambio. Volver de 'T' a 'Z' deja
-- el saldo igual al importe — indistinguible de una factura recién emitida —
-- y once salieron rotuladas "11 facturas nuevas · MLZ". Ninguna se emitió.
--
-- El código nuevo guarda `metadata.ids_facturas` en cada reverso. Esta
-- migración se lo repone a los reversos que ya estaban escritos, leyendo los
-- ids del snapshot `antes` del totalizar que deshicieron. No toca ninguna
-- factura ni ningún importe: sólo metadata, y sólo para que la pantalla
-- cuente bien lo que ya pasó.

UPDATE scintela.mov_doble r
   SET metadata = COALESCE(r.metadata, '{}'::jsonb)
                  || jsonb_build_object('ids_facturas', ids.arr)
  FROM (SELECT o.id_mov_doble,
               jsonb_agg((f->>'id')::bigint) AS arr
          FROM scintela.mov_doble o,
               jsonb_array_elements(o.metadata->'antes') f
         WHERE o.tipo = 'totalizar_estado_cuenta'
         GROUP BY o.id_mov_doble) ids
 WHERE r.tipo = 'reverso_totalizar_estado_cuenta'
   AND (r.metadata->>'id_mov_deshecho')::bigint = ids.id_mov_doble
   AND NOT (r.metadata ? 'ids_facturas');
