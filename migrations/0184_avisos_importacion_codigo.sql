-- 0184 — el link de la novedad de importación filtra con `codigo`, no con `q`.
--
-- 🚨 La 0183 dejó "/compras?q=<numero>" y el link se abría VACÍO ("Sin compras
-- en el filtro"): `q` es la búsqueda de TEXTO y no mira la columna `numero`.
-- El buscador flexible de /compras es `codigo` (letras = proveedor, dígitos =
-- número de compra). Otra vez lo mismo: un link puede estar roto sin dar 404,
-- y sólo se ve abriéndolo.
--
-- Verificado en producción: /compras?codigo=10145 trae UNA fila (AC 33,
-- 22.992,64 kg, $ 73.983,89).
UPDATE scintela.aviso
   SET url = replace(url, '/compras?q=', '/compras?codigo=')
 WHERE clave LIKE 'importaciones:compra:%'
   AND url LIKE '/compras?q=%';
