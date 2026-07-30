-- 0141 — los avisos de importaciones que el buzón se comió, al buzón.
--
-- TMT 2026-07-30: `avisos.avisar()` hacía el INSERT con `db.fetch_one()`, que
-- NO commitea → psycopg2 le hacía rollback al devolver la conexión al pool.
-- Resultado: `scintela.aviso` VACÍA desde que se estrenó y la campanita muda,
-- aunque los procesos avisaran bien. El código ya está arreglado
-- (`execute_returning`); esto recupera lo que pasó mientras tanto.
--
-- Sólo importaciones (es el único proceso que dejó su propio historial, en
-- `autobap_log`, así que se puede reconstruir sin inventar nada) y sólo las
-- CONVERSIONES de los últimos 3 días. El título sale del mensaje ya redactado
-- ("IM-0000570 · AI · $ 81.574,15 · Se cargó a compras"), sin re-formatear
-- plata en SQL. Idempotente por la clave.

INSERT INTO scintela.aviso
       (fuente, nivel, titulo, detalle, importe, cantidad, url, clave, leido,
        creado_en)
SELECT 'importaciones',
       'ok',
       LEFT(TRIM(REPLACE(l.mensaje, ' · Se cargó a compras', '')), 200),
       'Se cargó a compras',
       l.importe,
       l.n_anticipos,
       '/importaciones/automatico',
       'importaciones:compra:' || l.id_compra::text,
       l.leido,
       l.creado_en
  FROM scintela.autobap_log l
 WHERE l.tipo = 'conversion'
   AND l.id_compra IS NOT NULL
   AND l.mensaje LIKE '%Se cargó a compras'
   AND l.creado_en >= now() - INTERVAL '3 days'
ON CONFLICT (clave) DO NOTHING;
