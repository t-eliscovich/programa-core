-- Migration 0034: seed de los 6 vendedores canónicos.
--
-- Pedido dueña 2026-05-19 (sesión v8): "hay 6 vendedores: PPR, EDG, SEP,
-- JQU, FL1 y RMY". La migración 0032 creó la tabla scintela.vendedor y
-- backfilleó los códigos que ya aparecían en scintela.cliente.vend. Esta
-- garantiza que los 6 oficiales estén siempre presentes aunque ningún
-- cliente los referencie todavía.
--
-- Si el código ya existe (creado por 0032 o por la dueña a mano), no
-- toca el nombre ni el % comisión. Idempotente.

INSERT INTO scintela.vendedor (codigo, nombre, pct_comision, activo)
VALUES
    ('PPR', '', 0, TRUE),
    ('EDG', '', 0, TRUE),
    ('SEP', '', 0, TRUE),
    ('JQU', '', 0, TRUE),
    ('FL1', '', 0, TRUE),
    ('RMY', '', 0, TRUE)
ON CONFLICT (codigo) DO NOTHING;
