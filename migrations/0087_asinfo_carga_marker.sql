-- 0087_asinfo_carga_marker.sql
-- TMT 2026-06-10 — decisión de la dueña sobre la cartera Asinfo:
--   "solo si alguien aprieta CARGAR al programa cuentan; si no, pertenecen
--    a la lista Asinfo sin cargar en PC. Si se hace una carga de dBase,
--    eso gana por sobre todo."
--
-- Convención de markers resultante:
--   'asinfo-carga'    = alguien apretó Cargar en /facturas/desde-asinfo →
--                       CUENTA en cartera/balance live. El sync la preserva,
--                       pero si el DBF trae la misma factura, dBase GANA
--                       (la copia asinfo se absorbe — ver import_dbf.py).
--   'asinfo-backfill' = carga automática/histórica (script backfill, remark
--                       masivo) → NO cuenta en cartera/balance live. Queda
--                       para auditoría y estado de cuenta histórico.
--   'dbf-import'      = vino del dBase → cuenta (fuente autoritativa).
--
-- 1. Trigger: el force ahora apunta a 'asinfo-carga' (si un usuario inserta
--    a mano una factura con numf_completo formato Asinfo, lo hizo a
--    propósito → cuenta). Los paths automáticos setean 'asinfo-backfill'
--    explícito ANTES del insert y el trigger los respeta.

CREATE OR REPLACE FUNCTION scintela.fn_factura_force_asinfo_backfill_marker()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.numf_completo IS NOT NULL
       AND NEW.numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$'
       AND COALESCE(NEW.usuario_crea, '')
           NOT IN ('asinfo-backfill', 'asinfo-carga', 'dbf-import')
    THEN
        NEW.usuario_crea := 'asinfo-carga';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- (el trigger trg_factura_force_asinfo_marker ya existe y apunta a esta fn)

-- 2. Las 3 facturas del 09/06 (remarcadas a backfill por la 0085) fueron una
--    carga manual bulk de tamara → bajo el criterio nuevo son 'asinfo-carga'
--    y vuelven a contar en cartera ($4.733,41).
UPDATE scintela.factura
   SET usuario_crea = 'asinfo-carga'
 WHERE numf_completo IN (
        '001-099-000177059',  -- NAI  $2.804,09
        '001-099-000177049',  -- FGJ  $730,94
        '001-099-000177035'   -- MNM  $1.198,38
       )
   AND usuario_crea = 'asinfo-backfill';

-- Smoke test inline: INSERT de usuario humano → debe forzar 'asinfo-carga'.
DO $$
DECLARE
    test_id INTEGER;
    test_user_crea TEXT;
BEGIN
    INSERT INTO scintela.factura
        (numf, fecha, codigo_cli, kg, importe, abono, saldo,
         stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
    VALUES (
        999999998, CURRENT_DATE, 'XXX', 0, 0.01, 0, 0.01,
        'Z', NULL, NULL, NULL,
        '001-099-000999998', NULL, 'tamara_test'
    )
    RETURNING id_factura INTO test_id;

    SELECT usuario_crea INTO test_user_crea
      FROM scintela.factura WHERE id_factura = test_id;

    IF test_user_crea <> 'asinfo-carga' THEN
        RAISE EXCEPTION
            'Trigger no forzó asinfo-carga: usuario_crea=% ', test_user_crea;
    END IF;

    DELETE FROM scintela.factura WHERE id_factura = test_id;
    RAISE NOTICE 'Smoke OK: usuario humano + formato Asinfo → asinfo-carga';
END $$;

-- 3. La factura ASA 177935 del 10/06 (cargada hoy con el botón; el trigger
--    viejo la marcó backfill) → asinfo-carga.
UPDATE scintela.factura
   SET usuario_crea = 'asinfo-carga'
 WHERE usuario_crea = 'asinfo-backfill'
   AND numf = 177935 AND UPPER(TRIM(codigo_cli)) = 'ASA'
   AND fecha = DATE '2026-06-10';
