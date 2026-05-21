-- Migration 0041: quitar STOP del cliente HECTOR BEDON (codigo_cli='BED').
--
-- Pedido dueña 2026-05-21: "quitame a bedon de stop!!!". El STOP había
-- quedado como residuo de un test/cleanup viejo (E2E #33 cheque depositar
-- + reverso). Idempotente: si BED ya no está en STOP, este UPDATE
-- afecta 0 filas y nada se rompe.
--
-- Deja anotación en observación para que la dueña vea por qué se removió.

UPDATE scintela.cliente
   SET stop = 'N',
       observacion = COALESCE(observacion || ' | ', '') ||
                    '[STOP off 2026-05-21 — limpieza pedida por la dueña]',
       usuario_modifica = 'migration_0041'
 WHERE codigo_cli = 'BED'
   AND stop = 'S';
