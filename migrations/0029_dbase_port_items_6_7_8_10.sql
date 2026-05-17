-- Migration 0029: port dBase items #6, #7, #8, #10.
--
-- ITEM #6 (boleta depósito BOLEPICH/BOLEIN, BANCOS.PRG L1250-1359).
--     Sólo template + endpoint nuevo. NO requiere cambios de esquema —
--     levantamos los cheques del día vía chequextransaccion + tx_bancarias.
--
-- ITEM #7 (Cheque XX reemplazo, BANCOS.PRG L266-305).
--     Usa la columna existente `scintela.cheque.id_cheque_padre` (ya está
--     en el schema base) para linkear viejo↔nuevo. La observación va a
--     `scintela.cheque.observacion` (migración 0025).
--     Esta migración sólo agrega un índice para acelerar las queries de
--     hijos por padre (hijos() en cheques/queries.py).
--
-- ITEM #8 (BAP, BANCOS.PRG L733-819).
--     No requiere DDL. Reusa scintela.compra (cuenta_pagada='A',
--     comprobante='BAP<n>') + scintela.dolares (UPDATE st='B' — paridad
--     dBase `REPLA ALL ST WITH 'B'`. TMT 2026-05-15 decisión #8 revirtió
--     una intermedia que usaba 'X').
--
-- ITEM #10 (EFECT cobros 3 semanas, MENU.PRG L1493-1522).
--     No requiere DDL. Lee de scintela.chequesxfact (fechaing) +
--     scintela.transacciones_bancarias (fecha, documento IN ('DE','AC')).
--
-- Idempotente. NO la corras automáticamente — está orquestada con la 0028
-- del otro agente.

-- ITEM #7: índice sobre id_cheque_padre para que `hijos()` no escanee
-- toda la tabla cuando hay miles de cheques. Idempotente.
CREATE INDEX IF NOT EXISTS cheque_id_cheque_padre_idx
    ON scintela.cheque (id_cheque_padre)
    WHERE id_cheque_padre IS NOT NULL;

COMMENT ON INDEX scintela.cheque_id_cheque_padre_idx IS
    'Índice parcial para acelerar lookup de cheques hijos '
    '(espejos de anticipo + reemplazos XX). Usado por '
    'cheques.queries.hijos() y la query de auditoría de reemplazos.';


-- ITEM #8: índice sobre transacciones_bancarias (fecha, documento, no_banco)
-- para acelerar el lookup de cheques de boleta + cobros bancarios del día.
-- Idempotente.
CREATE INDEX IF NOT EXISTS tx_bancarias_fecha_doc_banco_idx
    ON scintela.transacciones_bancarias (fecha, documento, no_banco);

COMMENT ON INDEX scintela.tx_bancarias_fecha_doc_banco_idx IS
    'Índice compuesto para queries por fecha + documento + banco. '
    'Usado por cheques.queries.boleta_deposito() (ITEM #6) y '
    'cobranzas.queries.cobros_matriz_3_semanas() (ITEM #10).';


-- ITEM #10: índice sobre chequesxfact.fechaing para que la matriz de
-- 3 semanas no escanee toda la tabla. Idempotente.
CREATE INDEX IF NOT EXISTS chequesxfact_fechaing_idx
    ON scintela.chequesxfact (fechaing);

COMMENT ON INDEX scintela.chequesxfact_fechaing_idx IS
    'Índice sobre fechaing para acelerar matrices/agendas de cobros '
    '(cobranzas.queries.cobros_matriz_3_semanas, MENU.PRG L1493 EFECT).';
