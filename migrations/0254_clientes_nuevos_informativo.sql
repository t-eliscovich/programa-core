-- 0254 · "Cliente nuevo" es informativo, no ⚠ Para mirar
--
-- TMT 2026-09-29 (dueña): *"clientes nuevos avisa pero no tiene que ser para
-- mirar, solo informativo"*. El código ya los escribe con nivel 'ok'
-- (modules/clientes/sync_asinfo.py y facturas/views.py), pero un aviso se
-- escribe UNA vez —la clave lo hace idempotente— así que los que ya estaban
-- en la campanita quedarían en alerta para siempre. Se les baja el nivel sin
-- tocar título ni leído.
UPDATE scintela.aviso
   SET nivel = 'ok'
 WHERE clave LIKE 'cliente-nuevo-%'
   AND nivel = 'alerta';
