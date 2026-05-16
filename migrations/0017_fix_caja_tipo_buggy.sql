-- =====================================================================
-- 0017_fix_caja_tipo_buggy
-- =====================================================================
-- Limpia las filas de scintela.caja escritas por el app entre
-- 2026-04-17 y 2026-04-30 con la convención de tipo INCORRECTA:
--
--   App buggy (Apr 17–30):  tipo='I' (ingreso) o tipo='E' con importe<0
--                           (egreso forzado a negativo)
--   Convención correcta:    tipo='E' (entrada/+) o tipo='S' (salida/−)
--                           con importe SIEMPRE positivo
--
-- El bug se documenta en docs/AUDIT_2026-04-30.md (sección CRITICAL).
-- El código fix vive en `modules/caja/queries.py::crear()` desde
-- 2026-04-30. Esta migración limpia la data acumulada en el ínterin.
--
-- INVARIANTE post-migración: ninguna fila de scintela.caja debería tener
-- tipo='I' ni importe<0. La query de verificación al final lo asegura.
--
-- IDEMPOTENCIA: si se corre dos veces, el segundo run no encuentra filas
-- que cumplan el WHERE (porque ya las arregló) y es no-op. La migración
-- runner igual la marca como aplicada por checksum.
-- =====================================================================

BEGIN;

-- 1) Filas tipo='I' (convención buggy "ingreso") → 'E' con importe positivo.
--    Estas filas NUNCA deberían haber tenido tipo='I'; son ingresos en
--    lenguaje de la fábrica.
UPDATE scintela.caja
   SET tipo            = 'E',
       importe         = ABS(importe),
       usuario_modifica = 'migration-0017',
       fecha_modifica  = CURRENT_TIMESTAMP
 WHERE tipo = 'I'
   AND importe IS NOT NULL;

-- 2) Filas tipo='E' (convención buggy "egreso") con importe<0 → 'S'
--    con importe positivo. Las filas tipo='E' con importe>=0 son
--    legacy correctas (entradas), no tocarlas.
UPDATE scintela.caja
   SET tipo             = 'S',
       importe          = ABS(importe),
       usuario_modifica = 'migration-0017',
       fecha_modifica   = CURRENT_TIMESTAMP
 WHERE tipo = 'E'
   AND importe < 0;

-- 2b) Cualquier OTRA fila con importe<0 (tipo='S', 'CB', NULL, o algún
--     otro código legacy) → simplemente convertir a positivo, sin tocar
--     el tipo. La convención del módulo es importe siempre positivo.
--     El primer run de esta migración detectó 5 filas tipo='S' con
--     importe<0 que cayeron acá. Probablemente double-encoding del
--     dBase original (S+importe negativo da el mismo signo final que
--     S+importe positivo, así que el saldo running quedaba correcto y
--     nunca se notó).
--     COALESCE(tipo,'') porque `NULL NOT IN ('I','E')` evalúa a NULL,
--     no a TRUE — sin esto, filas con tipo NULL se escaparían.
UPDATE scintela.caja
   SET importe          = ABS(importe),
       usuario_modifica = 'migration-0017',
       fecha_modifica   = CURRENT_TIMESTAMP
 WHERE importe < 0
   AND COALESCE(tipo, '') NOT IN ('I', 'E');

-- 3) Recalcular running saldo desde la fila más vieja modificada.
--    No alcanza con arreglar tipo + importe — el campo `saldo` (running
--    balance) en cada fila también está mal porque se calculó con el
--    importe firmado buggy. Usamos una window function para reconstruir.
--
--    Lógica: ordenar por (fecha, id_caja) ASC, sumar cada movimiento
--    firmado por tipo, partiendo del opening balance que tenemos en
--    la primera fila de la tabla (es estable, ya estaba bien).
WITH primer_opening AS (
    -- saldo de la primera fila MENOS su movimiento firmado = opening real.
    SELECT saldo - CASE
              WHEN tipo = 'E' THEN importe
              WHEN tipo = 'S' THEN -importe
              ELSE importe
           END AS opening
      FROM scintela.caja
     WHERE saldo IS NOT NULL
     ORDER BY fecha ASC, id_caja ASC
     LIMIT 1
),
recalculado AS (
    SELECT id_caja,
           (SELECT opening FROM primer_opening) +
               SUM(CASE WHEN tipo = 'E' THEN importe
                        WHEN tipo = 'S' THEN -importe
                        ELSE importe END)
               OVER (ORDER BY fecha ASC, id_caja ASC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
               AS saldo_recalc
      FROM scintela.caja
)
UPDATE scintela.caja c
   SET saldo            = r.saldo_recalc,
       usuario_modifica = 'migration-0017',
       fecha_modifica   = CURRENT_TIMESTAMP
  FROM recalculado r
 WHERE c.id_caja = r.id_caja
   AND ABS(COALESCE(c.saldo, 0) - r.saldo_recalc) > 0.01;

-- 4) VERIFICACIÓN: si quedó alguna fila violando el invariante, abortamos
--    y rolleamos. La migración no se marca aplicada y el deploy falla
--    fuerte para que se investigue.
DO $$
DECLARE
    n_buggy_tipo INT;
    n_buggy_neg  INT;
BEGIN
    SELECT COUNT(*) INTO n_buggy_tipo FROM scintela.caja WHERE tipo = 'I';
    SELECT COUNT(*) INTO n_buggy_neg  FROM scintela.caja WHERE importe < 0;
    IF n_buggy_tipo > 0 OR n_buggy_neg > 0 THEN
        RAISE EXCEPTION
            'migration-0017: post-fix quedaron % filas con tipo=I y % con importe<0. Abortando.',
            n_buggy_tipo, n_buggy_neg;
    END IF;
END $$;

COMMIT;
