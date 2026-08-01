-- Borra los avisos "import-sin-plata" de importaciones viejas.
--
-- TMT 2026-07-31, mirando los 8 que quedaron (de 37 a 225 días): *"borrá todo
-- lo que ya pasó más de 31 días, no tiene sentido traer problemas de hace tanto
-- tiempo"*.
--
-- El razonamiento es de producto, no técnico, y es correcto: con la alarma
-- andando NO hay backlog. Cada importación se agarra al cruzar los 30 días y
-- nunca llega a ser vieja. Un aviso sobre algo de hace siete meses no es una
-- tarea: es un inventario histórico, y llena la campanita que la dueña pidió
-- justamente para no tener ruido.
--
-- Los 8 de hoy tienen entre 37 y 225 días — o sea que se van todos. Los casos
-- no desaparecen del sistema, sólo dejan de avisar: se siguen viendo en
-- /admin/importaciones-sin-plata?techo=0.
--
-- El código (modules/importaciones/vigilancia.py, `_MAX_DIAS_DEFAULT = 31`) ya
-- no los genera. Sin este DELETE quedarían pegados para siempre, porque el
-- aviso es idempotente por `clave` y nada los limpia solo.
--
-- Sólo toca filas de esta alarma. Ningún otro aviso se ve afectado.

DELETE FROM scintela.aviso
 WHERE clave LIKE 'import-sin-plata:%';
