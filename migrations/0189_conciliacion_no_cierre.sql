-- 0189_conciliacion_no_cierre.sql
-- ⚠ RENUMERADA el 2026-08-13. Nació como `0062_conciliacion_no_cierre` y
-- NUNCA CORRIÓ: producción ya tenía el número 0062 tomado por
-- `0062_fix_yy_baseline_zona_ec`, y el migrador lleva la cuenta por NÚMERO,
-- así que la dio por aplicada y la salteó en silencio durante ~2 meses.
-- Verificado el 13/08 contra la base: el índice único seguía siendo el viejo
-- `(no_banco, usuario)`, o sea que dos usuarios podían tener cada uno su
-- conciliación abierta del mismo banco al mismo tiempo. El contenido es el
-- original, sin tocar. Ver `scripts/migrate.py`, que desde hoy corta el deploy
-- si vuelve a pasar.

-- Eliminar el concepto de "cerrar sesión" de conciliación bancaria — 2026-06-02.
--
-- TMT 2026-06-02 dueña: 'no quiero cerrar la sesion, quiero dejar seguir
-- editando. borremos lo necesario.' La sesión vive para siempre. Cada
-- upload de extracto se mergea por número de documento (sin duplicar) a
-- la única sesión abierta del banco.
--
-- Cambios:
--   1. Cerrar (con marca mig-0062-auto-close) todas las sesiones abiertas
--      excepto la más reciente por banco. Mantenemos las cerradas como
--      registro histórico.
--   2. Reemplazar el unique parcial (no_banco, usuario) WHERE cerrada_en
--      IS NULL → (no_banco) WHERE cerrada_en IS NULL. Una sesión activa
--      por banco, sin importar usuario.

-- 1) Dejar UNA sola sesión abierta por banco (la más reciente).
WITH ranked AS (
    SELECT id, no_banco,
           ROW_NUMBER() OVER (
               PARTITION BY no_banco
               ORDER BY abierta_en DESC, id DESC
           ) AS rn
      FROM scintela.banco_conciliacion_sesion
     WHERE cerrada_en IS NULL
)
UPDATE scintela.banco_conciliacion_sesion s
   SET cerrada_en = CURRENT_TIMESTAMP,
       cerrada_por = 'mig-0062-auto-close'
  FROM ranked r
 WHERE s.id = r.id
   AND r.rn > 1;

-- 2) Drop el unique parcial viejo + crear el nuevo.
DROP INDEX IF EXISTS scintela.banco_conciliacion_sesion_abierta_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS banco_conciliacion_sesion_abierta_uniq
    ON scintela.banco_conciliacion_sesion (no_banco)
 WHERE cerrada_en IS NULL;
