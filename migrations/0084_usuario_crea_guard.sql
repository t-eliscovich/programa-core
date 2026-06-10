-- 0084_usuario_crea_guard.sql
-- TMT 2026-06-10 — Capa 2 de protección contra el bug "utilidad inflada".
--
-- Bug original: facturas cargadas vía /facturas/cargar-desde-asinfo* quedaron
-- con usuario_crea = current_user (tamara/andres/alex) en vez del marker
-- canónico 'asinfo-backfill'. Los filtros NO_BACKFILL_WHERE del balance live
-- las contaron como facturas normales → cartera infló +$500k → utilidad
-- infló +$420k.
--
-- Defensa: trigger BEFORE INSERT/UPDATE en scintela.factura que detecta
-- automáticamente las filas con `numf_completo` en formato Asinfo y FUERZA
-- `usuario_crea = 'asinfo-backfill'` (sobreescribe el que vino del caller).
--
-- Filas detectadas como Asinfo:
--   numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$'   (XXX-XXX-XXXXXXXXX)
--
-- Excepciones:
--   - Si usuario_crea = 'dbf-import': preservar (sync dBase es la fuente
--     histórica autoritativa; no quiere recategorizar a backfill).
--   - Si usuario_crea = 'asinfo-backfill' ya: no-op.
--
-- Por qué solo factura y no compra/dolares:
--   scintela.compra y scintela.dolares no tienen un campo equivalente a
--   numf_completo con formato Asinfo distintivo. Si el bug aparece en esas
--   tablas, hay que detectar por otra heurística — agregar trigger separado.
--
-- # TMT decisión 2026-06-10: trigger ACTIVO (rewrites) en vez de pasivo
-- (warning + log). El active rewrite es más fuerte pero podría tapar bugs
-- legítimos donde un usuario QUIERE cargar una factura con un numero Asinfo
-- "casualmente" en formato. Aceptamos ese trade-off: el formato XXX-XXX-X{9}
-- es Asinfo-específico de Ecuador SRI — uso fuera de Asinfo es muy raro.

CREATE OR REPLACE FUNCTION scintela.fn_factura_force_asinfo_backfill_marker()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.numf_completo IS NOT NULL
       AND NEW.numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$'
       AND COALESCE(NEW.usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import')
    THEN
        NEW.usuario_crea := 'asinfo-backfill';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_factura_force_asinfo_marker
    ON scintela.factura;

CREATE TRIGGER trg_factura_force_asinfo_marker
    BEFORE INSERT OR UPDATE OF numf_completo, usuario_crea
    ON scintela.factura
    FOR EACH ROW
    EXECUTE FUNCTION scintela.fn_factura_force_asinfo_backfill_marker();

-- Owner — la función la crea el migrate runner; si corre como postgres y
-- la app usa otro user, el trigger podría no verse. Aseguramos owner =
-- current_user para que el grant funcione en cualquier rol.
DO $$
DECLARE u TEXT := current_user;
BEGIN
    IF u <> 'postgres' THEN
        EXECUTE format(
            'ALTER FUNCTION scintela.fn_factura_force_asinfo_backfill_marker() OWNER TO %I',
            u
        );
    END IF;
END $$;

-- Smoke test inline — verificar que el trigger funciona con un INSERT
-- transitorio (rollback al final, no deja huella).
DO $$
DECLARE
    test_id INTEGER;
    test_user_crea TEXT;
BEGIN
    -- INSERT con usuario_crea='tamara' (el escenario del bug)
    INSERT INTO scintela.factura
        (numf, fecha, codigo_cli, kg, importe, abono, saldo,
         stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
    VALUES (
        999999999, CURRENT_DATE, 'XXX', 0, 0.01, 0, 0.01,
        'Z', NULL, NULL, NULL,
        '001-099-000999999', NULL, 'tamara_test'
    )
    RETURNING id_factura INTO test_id;

    SELECT usuario_crea INTO test_user_crea
      FROM scintela.factura WHERE id_factura = test_id;

    IF test_user_crea <> 'asinfo-backfill' THEN
        RAISE EXCEPTION
            'Trigger fn_factura_force_asinfo_backfill_marker NO funcionó: '
            'usuario_crea=% (esperaba asinfo-backfill)', test_user_crea;
    END IF;

    -- Cleanup: borrar la fila de prueba
    DELETE FROM scintela.factura WHERE id_factura = test_id;
    RAISE NOTICE 'Trigger smoke test OK: usuario_crea forzado a asinfo-backfill';
END $$;
