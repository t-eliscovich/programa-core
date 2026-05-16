-- =====================================================================
-- 0013_estados_canonicos
-- =====================================================================
-- Captura el vocabulario canónico de stats acordado con el dueño
-- (ver docs/SKILL_ADDENDUM_BATCH_18.md). Tres cambios independientes:
--
-- 1. `cheque.fecha_recibido date` — cuándo físicamente recibimos el
--    cheque. Hasta hoy se asumía implícitamente que era la fecha de
--    creación (`fecha_crea::date`). Ahora se captura explícitamente.
--    Backfill: COALESCE(fecha_crea::date, fecha) para filas existentes.
--
-- 2. `COMMENT ON COLUMN cheque.stat` y `factura.stat` — los códigos
--    canónicos quedan documentados a nivel schema para que cualquier
--    persona que mire la DB con `\d+` o un cliente SQL los vea sin
--    tener que abrir el repo.
--
-- 3. `factura.stat` deprecation note — 'Y' (legacy "anulada") se
--    renombra a 'X' (eliminada por error). Las filas históricas con
--    'Y' quedan como están — son históricos pre-rename. El código
--    nuevo escribe 'X'.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS, COMMENT siempre se puede
-- re-ejecutar.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. cheque.fecha_recibido
-- ---------------------------------------------------------------------
ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS fecha_recibido date;

-- Backfill sólo donde está NULL (idempotente). Usamos COALESCE de
-- fecha_crea::date primero (es el dato más cercano a "cuándo lo
-- ingresaron"), y fallback a `fecha` (la del cheque) por si no hay
-- fecha_crea (filas migradas del dBase pueden tener fecha_crea NULL).
UPDATE scintela.cheque
   SET fecha_recibido = COALESCE(fecha_crea::date, fecha)
 WHERE fecha_recibido IS NULL;

CREATE INDEX IF NOT EXISTS idx_cheque_fecha_recibido
    ON scintela.cheque (fecha_recibido);

COMMENT ON COLUMN scintela.cheque.fecha_recibido IS
    'Fecha en la que físicamente recibimos el cheque (puede ser <= fechad). '
    'Distinta de `fecha` (escrita en el papel) y de `fechad` (a depositar).';

-- ---------------------------------------------------------------------
-- 2. cheque.stat — remap de legacy 'D' (depositado genérico) a 'B'
-- ---------------------------------------------------------------------
-- En el sistema legacy, 'D' significaba "depositado en banco" sin
-- distinción de cuál. La fábrica deposita ~siempre en Pichincha
-- (no_banco=1). Los pocos casos de Internacional usaban 'V'. En el
-- nuevo vocabulario:
--    'B' = depositado en Pichincha (estado terminal feliz)
--    'D' = Daniela (gestión de cobranza)
-- El conflicto sobre 'D' se resuelve aquí: todas las filas con
-- stat='D' pasan a stat='B'. Después de esta migración, la única
-- semántica de 'D' es Daniela.
--
-- Idempotente: si no quedan 'D' (ya se corrió antes), el UPDATE
-- no hace nada.
-- ---------------------------------------------------------------------
UPDATE scintela.cheque
   SET stat = 'B',
       fecha_modifica = CURRENT_TIMESTAMP,
       usuario_modifica = COALESCE(usuario_modifica, 'migracion-0013')
 WHERE stat = 'D';

-- ---------------------------------------------------------------------
-- 3. cheque.stat — comentario canónico
-- ---------------------------------------------------------------------
COMMENT ON COLUMN scintela.cheque.stat IS
    'Vocabulario canónico (2026-04-29):'
    ' Z=cartera (ingresado, no pasó nada);'
    ' B=depositado en banco Pichincha (terminal feliz);'
    ' V=banco Internacional (LEGACY, no usar para nuevos);'
    ' 1=devuelto/rechazado #1 (sólo desde B);'
    ' 2=devuelto/rechazado #2 (alias de 1);'
    ' 3=segundo rechazo (sólo desde 1);'
    ' D=Daniela (gestión de cobranza);'
    ' P=postergado (sólo desde Z, requiere fechad nueva).'
    ' Reglas: alta siempre Z. Solo Z puede ir a B. Solo B a 1/2. Solo 1 a 3.'
    ' Solo Z a P.';

-- ---------------------------------------------------------------------
-- 4. factura.stat — comentario canónico
-- ---------------------------------------------------------------------
COMMENT ON COLUMN scintela.factura.stat IS
    'Vocabulario canónico (2026-04-29):'
    ' Z=emitida (sin abono);'
    ' A=abonada parcial (saldo > 0);'
    ' T=cancelada por el total (saldo = 0, terminal feliz);'
    ' X=eliminada por error (anulación administrativa).'
    ' DEPRECADO: ''Y'' (anulada) → migrar a ''X''. Históricos con Y se respetan.'
    ' Cartera = Z + A. La transición Z→A→T se calcula desde chequesxfact.';
