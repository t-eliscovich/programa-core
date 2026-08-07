-- 0179 — La RETENCIÓN deja de vivir escondida adentro del abono.
--
-- Pedido de la dueña (07/08/2026): "necesitaríamos dividir esto en dos
-- columnas: retenciones y abono, no todo junto (entonces no se debería sumar
-- más)". Revierte la decisión del 06/08 (commit 8ed70dd, "la retención SIEMPRE
-- suma al abono") en cuanto a DÓNDE se guarda — no en cuanto al efecto sobre
-- la deuda del cliente: la retención sigue bajando el saldo.
--
-- Modelo nuevo:      saldo = importe − abono − retencion
-- Modelo viejo:      saldo = importe − abono            (con la retención
--                                                        adentro del abono)
--
-- El SALDO no cambia en ninguna factura: lo que estaba adentro de `abono` se
-- muda a `retencion`, y la resta da lo mismo. Por eso el backfill usa
-- LEAST(rete, abono): así `importe − abono_nuevo − retencion` sigue siendo
-- EXACTAMENTE el saldo guardado, incluso en las filas raras donde la
-- retención registrada es mayor que el abono (esas quedan con la retención
-- topeada y se miran a mano; el invariante no se rompe).

ALTER TABLE scintela.factura
    ADD COLUMN IF NOT EXISTS retencion numeric(14,2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN scintela.factura.retencion IS
    'Retención en la fuente del cliente (fuente+IVA). Baja el saldo igual que '
    'un abono pero NO se suma a `abono`: saldo = importe − abono − retencion. '
    'Espejo de SUM(scintela.retencion.rete) por (codigo_cli, numf). '
    'Migración 0179, 2026-08-07.';

-- Backfill: sacar del abono lo que ya estaba adentro.
WITH r AS (
    SELECT codigo_cli, numf, SUM(COALESCE(rete, 0)) AS rete
      FROM scintela.retencion
     GROUP BY codigo_cli, numf
)
UPDATE scintela.factura f
   SET retencion = LEAST(r.rete, COALESCE(f.abono, 0)),
       abono     = COALESCE(f.abono, 0) - LEAST(r.rete, COALESCE(f.abono, 0))
  FROM r
 WHERE r.codigo_cli = f.codigo_cli
   AND r.numf       = f.numf
   AND r.rete > 0
   AND COALESCE(f.retencion, 0) = 0;

CREATE INDEX IF NOT EXISTS idx_factura_retencion
    ON scintela.factura (codigo_cli, numf)
 WHERE retencion > 0;
