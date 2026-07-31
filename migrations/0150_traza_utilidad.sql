-- 0150 — LA GRABADORA: una foto del balance cada pocos minutos.
--
-- TMT 2026-07-31 (dueña): *"mirálo ahora. guardá la data y fijate en un rato
-- también. no podemos tener utilidad menor a la mañana"*.
--
-- Todo el día se discutió por qué la utilidad se movía, y cada vez la respuesta
-- fue una hipótesis: no había con qué comparar. `scintela.historia` guarda UNA
-- fila por día, así que un salto de las 09:16 a las 10:34 no deja rastro en
-- ningún lado y hay que reconstruirlo de memoria.
--
-- Esta tabla es el rastro: cada N minutos se guarda la utilidad JUNTO CON sus
-- componentes. Cuando salta, se mira la fila y se ve QUÉ se movió —el stock,
-- los anticipos, la cartera, los pasivos— en vez de adivinar. Es append-only y
-- no la lee nadie del balance: si se cae, no pasa nada.
--
-- Los kg y la tarifa del hilado van aparte porque el stock se valúa a promedio
-- ponderado: la tarifa mueve el valor de TODO el stock, así que hay que poder
-- distinguir "entraron/salieron kilos" de "cambió el $/kg".

CREATE TABLE IF NOT EXISTS scintela.traza_utilidad (
    id_traza      BIGSERIAL   PRIMARY KEY,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    origen        TEXT        NOT NULL DEFAULT 'loop',  -- loop | manual | test

    -- El número que se mira
    utilidad      NUMERIC(16, 2),
    patr_neto     NUMERIC(16, 2),

    -- Sus componentes, en el mismo orden del balance
    caja          NUMERIC(16, 2),
    bancos        NUMERIC(16, 2),
    cheques       NUMERIC(16, 2),
    facturas      NUMERIC(16, 2),
    antic         NUMERIC(16, 2),
    vsto          NUMERIC(16, 2),
    vqx           NUMERIC(16, 2),
    umaq          NUMERIC(16, 2),
    uact          NUMERIC(16, 2),
    totp          NUMERIC(16, 2),
    uret          NUMERIC(16, 2),

    -- Stock en kilos + la tarifa que lo valúa (mueve TODO el stock de una)
    hilado_kg     NUMERIC(16, 2),
    hilado_ukg    NUMERIC(12, 4),
    tejido_kg     NUMERIC(16, 2),
    terminado_kg  NUMERIC(16, 2),

    -- Compras de hilado del mes: kilos que ya tienen su plata y los que no
    compras_kg    NUMERIC(16, 2),
    compras_us    NUMERIC(16, 2),
    kg_sin_costo  NUMERIC(16, 2),

    -- Ventas acumuladas del mes (para separar "vendimos más" de "algo saltó")
    venta_kg      NUMERIC(16, 2),
    venta_us      NUMERIC(16, 2)
);

CREATE INDEX IF NOT EXISTS ix_traza_utilidad_creado
    ON scintela.traza_utilidad (creado_en DESC);
