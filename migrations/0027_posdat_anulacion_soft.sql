-- Migration 0027: anulación lógica (soft-delete) de scintela.posdat.
--
-- Contexto (TMT 2026-05-14, audit bloque B): hasta hoy `posdat.anular()`
-- hacía un DELETE físico. Eso destruía la traza histórica y no chequeaba
-- side-effects (p. ej. si la posdat ya había sido pagada con un cheque
-- emitido — banc<>0 — la deuda desaparecía pero el cheque seguía colgado).
--
-- Esta migración agrega tres columnas para soft-delete con trazabilidad:
--
--   anulada            BOOLEAN   default FALSE — la fila está anulada.
--   motivo_anulacion   VARCHAR(200) — qué dijo el usuario al anular.
--   fecha_anulacion    TIMESTAMP — cuándo se anuló.
--
-- A partir de ahora `posdat.anular()` hace UPDATE en vez de DELETE, todas
-- las queries de listado/balance filtran `anulada IS NOT TRUE`, y se
-- bloquea la anulación si banc<>0 (la posdat ya está instrumentada con
-- cheque/banco — primero hay que reversar el cheque emitido).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS. Las filas existentes quedan con
-- anulada=NULL (equivalente a FALSE en los filtros COALESCE).

ALTER TABLE scintela.posdat
    ADD COLUMN IF NOT EXISTS anulada          BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS motivo_anulacion VARCHAR(200),
    ADD COLUMN IF NOT EXISTS fecha_anulacion  TIMESTAMP;

-- Backfill: si quedó alguna fila con NULL (por DEFAULT FALSE no aplicado
-- en updates futuros del propio script), forzamos FALSE explícito así los
-- filtros `anulada IS NOT TRUE` no dependen de COALESCE.
UPDATE scintela.posdat SET anulada = FALSE WHERE anulada IS NULL;

CREATE INDEX IF NOT EXISTS idx_posdat_anulada
    ON scintela.posdat (anulada)
    WHERE anulada = TRUE;

COMMENT ON COLUMN scintela.posdat.anulada IS
    'Soft-delete flag. TRUE = anulada (no se cuenta en listados ni balances). '
    'Reemplaza el DELETE físico de versiones anteriores.';

COMMENT ON COLUMN scintela.posdat.motivo_anulacion IS
    'Razón que dio el usuario al anular. Requerido (>= 10 chars) desde TMT '
    '2026-05-14 — antes era opcional y se perdía la traza del por qué.';

COMMENT ON COLUMN scintela.posdat.fecha_anulacion IS
    'CURRENT_TIMESTAMP al momento del UPDATE de anulación. NULL si la fila '
    'no fue anulada.';
