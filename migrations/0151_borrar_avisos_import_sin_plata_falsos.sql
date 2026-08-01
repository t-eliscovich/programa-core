-- Borra los avisos "import-sin-plata" del 31/07/2026.
--
-- La primera versión de la vigilancia de importaciones sin plata no tenía techo
-- de antigüedad ni exigía que hubiera AL MENOS UN movimiento cargado. Para una
-- importación de 2025 el cruce no encuentra ninguna compra ni anticipo — los
-- anticipos ya se convirtieron (dejan de estar vivos) y PC no tiene las compras
-- de esa época — así que el US$/kg da 0,00 y la alarma lo leyó como "no
-- cargaron nada". No era eso: era "PC nunca tuvo ese dato".
--
-- Resultado: 200+ avisos en la campanita a los tres minutos de deployar, sobre
-- importaciones de septiembre de 2025 en adelante. La dueña pidió esta alarma
-- justamente para NO tener ruido ("nada de rojo en resultados, en la
-- campanita"), así que el ruido acá es peor que no tenerla.
--
-- Se borran TODOS los de esa clave, no sólo los falsos: el código corregido
-- (modules/importaciones/vigilancia.py, con `_MAX_DIAS` y el mínimo de un
-- movimiento) los vuelve a crear en la siguiente corrida — pero sólo los ~6
-- reales. La `clave` es única, así que sin borrar no se regeneran.
--
-- Sólo toca filas de esta alarma. Ningún otro aviso se ve afectado.

DELETE FROM scintela.aviso
 WHERE clave LIKE 'import-sin-plata:%';
