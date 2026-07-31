-- 0149 — los avisos VIEJOS de importaciones se renombran al CÓDIGO (`AC 17`).
--
-- TMT 2026-07-31 (dueña, mirando la campanita): *"acá tampoco tengo el número
-- de AC, agregalo"*. El título de los avisos nuevos ya sale bien desde
-- `autobap.resumen()` (SHA 477cebd9), pero `scintela.aviso` guarda el TEXTO
-- congelado al momento del aviso: las filas anteriores al deploy siguen
-- diciendo `IM-0000561 · AI · $ 73.851,68`. El historial de
-- /importaciones/automatico sí se re-renderiza, y por eso los dos lados decían
-- cosas distintas del MISMO evento.
--
-- El código sale del log del automático, que ya lo tenía guardado: la clave del
-- aviso es `importaciones:compra:<id_compra>`, que es la PK con la que
-- `scintela.autobap_log` conoce a esa misma conversión.
--
-- Sólo toca los títulos con la forma vieja exacta (`IM-… · PROV · $ …`), y
-- conserva el importe tal cual estaba escrito. Idempotente: después de correr,
-- ningún título arranca con `IM-` y el UPDATE no matchea nada.

UPDATE scintela.aviso a
   SET titulo = UPPER(TRIM(l.codigo_prov)) || ' ' || l.ref_num
                || ' · ' || split_part(a.titulo, ' · ', 3)
  FROM scintela.autobap_log l
 WHERE a.clave = 'importaciones:compra:' || l.id_compra
   AND a.fuente = 'importaciones'
   AND a.titulo LIKE 'IM-%'
   AND split_part(a.titulo, ' · ', 3) LIKE '$ %'
   AND NULLIF(TRIM(COALESCE(l.codigo_prov, '')), '') IS NOT NULL
   AND l.ref_num IS NOT NULL;
