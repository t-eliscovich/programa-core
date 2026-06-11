-- 0099_banco_66_banecuador.sql
-- TMT 2026-06-11 — pedido dueña: "66 codigo para BANECUADOR en cheques
-- falta crear". Banco emisor nuevo para el dropdown de Nueva Cobranza /
-- cheques (scintela.banco es data estática de PC, el sync dBase NO la
-- toca, así que el insert persiste).
--
-- IDEMPOTENTE: WHERE NOT EXISTS (prod no tiene UNIQUE(no_banco) garantizado
-- → no usamos ON CONFLICT). Guard to_regclass: si la tabla no existe
-- (DB de test vacía) la mig no explota.
DO $$
BEGIN
  IF to_regclass('scintela.banco') IS NOT NULL THEN
    INSERT INTO scintela.banco (no_banco, nombre, usuario_crea)
    SELECT 66, 'BANECUADOR', 'mig-0099'
     WHERE NOT EXISTS (
           SELECT 1 FROM scintela.banco WHERE no_banco = 66
     );
  END IF;
END $$;
