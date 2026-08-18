-- 0196_parado_saca_tela_cruda.sql
-- Sacar TELA CRUDA de la cohorte de parados.
-- Dueña 17/08/2026: "borra tela cruda".
--
-- ⭐ Hace falta una migración —y no alcanza con el filtro de la 0195— porque la
-- cohorte es DELIBERADAMENTE inmutable: el refresh sólo inserta, nunca borra,
-- para que un ítem que se vendió no desaparezca de la lista. Esa regla es la
-- que hace útil la pantalla, y también la que deja adentro para siempre algo
-- que entró por error. Cuando hay que sacar algo, se saca acá y a mano.
--
-- Son 2 tela×color, 22 kg. Nunca debieron entrar: tela sin teñir que pasó por
-- la bodega de producto terminado.

DELETE FROM scintela.parado_foto
 WHERE subcategoria IN (
     SELECT subcategoria FROM scintela.parado_foto WHERE categoria = 'TELA CRUDA');

DELETE FROM scintela.parado_cohorte c
 WHERE NOT EXISTS (SELECT 1 FROM scintela.parado_foto f
                    WHERE f.subcategoria = c.subcategoria AND f.color = c.color)
   AND c.subcategoria IN ('Tela Cruda', 'TELA CRUDA');

-- Por las dudas de que la categoría todavía no esté poblada (la 0195 es nueva):
DELETE FROM scintela.parado_cohorte
 WHERE upper(subcategoria) LIKE 'TELA CRUDA%';
