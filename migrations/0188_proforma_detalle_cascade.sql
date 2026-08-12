-- Migration 0188: que borrar una proforma se lleve su detalle.
--
-- 11/08/2026, encontrado EN PRODUCCIÓN por el bloque `proformas_purga` del
-- health, la primera vez que corrió:
--
--     update or delete on table "proforma_cabecera" violates foreign key
--     constraint "proforma_detalle_id_proforma_fkey"
--     DETAIL: Key (id_proforma)=(1) is still referenced from proforma_detalle
--
-- La migración 0119 declara la FK con ON DELETE CASCADE, pero adentro de un
-- `CREATE TABLE IF NOT EXISTS`: en producción las tablas YA EXISTÍAN (las creó
-- alguien a mano antes de que hubiera migración — está contado en el header de
-- la 0119), así que ese CREATE no hizo nada y la FK quedó sin cascade. En
-- local, donde la base la construye la migración, la cascade sí estaba: por eso
-- los tests pasaban.
--
-- ⭐ La lección para la próxima: un `CREATE TABLE IF NOT EXISTS` NO garantiza
-- la forma de la tabla, sólo su existencia. Si la tabla puede preexistir, cada
-- constraint importante necesita su propio ALTER idempotente.

ALTER TABLE scintela.proforma_detalle
    DROP CONSTRAINT IF EXISTS proforma_detalle_id_proforma_fkey;

ALTER TABLE scintela.proforma_detalle
    ADD CONSTRAINT proforma_detalle_id_proforma_fkey
    FOREIGN KEY (id_proforma)
    REFERENCES scintela.proforma_cabecera(id_proforma)
    ON DELETE CASCADE;
