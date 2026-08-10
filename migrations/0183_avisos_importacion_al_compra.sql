-- 0183 — el "ver →" de las novedades de importación lleva a LA compra.
--
-- 🚨 TMT 2026-08-09, sobre "AC 33 · 22.993 kg · $ 73.983,89 · Se cargó a
-- compras · ver →": *"el link acá de ver me debería llevar a la pantalla
-- compras ya filtrada. No acá: /importaciones/automatico"*.
--
-- El aviso dice "se cargó a compras" y llevaba a la pantalla del proceso,
-- donde hay que volver a buscar la compra a ojo. La `clave` del aviso ya
-- guarda el id ("importaciones:compra:<id>"), así que las viejas se
-- reapuntan sin adivinar nada: el buscador de /compras toma los dígitos
-- como número de compra.
--
-- Sólo las conversiones. Los frenos y los errores SÍ son del proceso y se
-- quedan donde están.
UPDATE scintela.aviso a
   SET url = '/compras?q=' || c.numero
  FROM scintela.compra c
 WHERE a.clave LIKE 'importaciones:compra:%'
   AND a.url = '/importaciones/automatico'
   AND c.id_compra = NULLIF(split_part(a.clave, ':', 3), '')::int
   AND COALESCE(c.numero, 0) <> 0;
