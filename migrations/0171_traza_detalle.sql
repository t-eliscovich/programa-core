-- 0171 — LA TRAZA BAJA A DOCUMENTO.
--
-- TMT 2026-08-06 (dueña, mirando /informes/traza): *"¿podés explicarme mejor el
-- movimiento exacto que causó la movida?"* y, sobre el stock: *"pero stock
-- también, por qué sube y por qué baja"*.
--
-- La grabadora (mig 0150) saca una foto cada 5 minutos y guarda los 11
-- componentes. Contesta QUÉ COMPONENTE se movió —"Facturas +11.399"— y ahí se
-- termina. La explicación del día (mig 0161) sí baja a documento, pero corre
-- DOS veces por día: entre las 07:00 y las 18:00 no hay con qué contestar
-- "¿qué pasó a las 16:14?".
--
-- Verificado el 06/08 contra las filas reales: el salto de las 16:14
-- (Facturas +11.399) fueron TRES facturas —GBC 162,17 · SAC 495,67 ·
-- AJT 10.741,46 = 11.399,30— y el de las 16:07 (+3.798) fueron SIETE, sumando
-- 3.798,00 exacto. La información está; lo que falta es guardarla.
--
-- Esta migración unifica las dos maquinarias en una sola:
--
--   traza_utilidad  — LA foto (cada 5 min y también las anclas del día). Ya
--                     existía; acá sólo se le agrega de qué momento es.
--   traza_detalle   — la foto RODANTE a nivel documento. Reemplaza a
--                     `dia_detalle`. Es la única tabla que se pisa, y a esta
--                     cadencia se actualiza POR DIFERENCIA (no se borra
--                     entera): 7.100 filas x 288 vueltas por día serían 2
--                     millones de escrituras diarias para mover, en una vuelta
--                     típica, menos de cincuenta.
--   dia_movimiento  — el diff, permanente. Pasa a colgar de `id_traza` en vez
--                     de `id_captura`: los movimientos existen cada 5 minutos,
--                     y una captura del día es sólo una de esas fotos marcada
--                     como ancla.
--   dia_captura     — sigue siendo el ancla del día (07:00 / 18:00) y la nota
--                     de la dueña. Ahora apunta a la foto de la traza que le
--                     corresponde, así la explicación del día es la SUMA de
--                     las ventanas de 5 minutos que caen adentro.
--
-- El invariante no cambia y se vuelve más exigente, que es el punto:
--
--     Δ utilidad = Σ (aporte de cada movimiento)      ← ahora cada 5 minutos
--
-- Un `#ajuste` que antes quedaba encerrado en una ventana de doce horas ahora
-- queda encerrado en una de cinco minutos. Eso es lo que lo hace cazable.

-- ── La foto rodante a nivel documento ──────────────────────────────────────
-- Misma forma que `dia_detalle` (mig 0161). `cantidad` y `precio` van aparte
-- del importe por el stock: se valúa a promedio ponderado, así que la tarifa
-- mueve el valor de TODO el stock de un saque y hay que poder distinguir
-- "entraron kilos" de "cambió el $/kg".
CREATE TABLE IF NOT EXISTS scintela.traza_detalle (
    componente  TEXT           NOT NULL,  -- caja|bancos|...|uret, o '#flujo'
    doc_id      TEXT           NOT NULL,  -- id interno, o '#...' si es sintética
    etiqueta    TEXT,                     -- lo que la dueña reconoce
    importe     NUMERIC(16, 2) NOT NULL,
    cantidad    NUMERIC(16, 2),           -- kg, cuando aplica
    precio      NUMERIC(12, 4),           -- $/kg, cuando aplica
    PRIMARY KEY (componente, doc_id)
);

-- Se hereda la foto que ya tenía la explicación del día: sin esto, la primera
-- vuelta de la traza diffearía contra la nada y cada factura viva de la
-- cartera saldría como "venta de este minuto".
INSERT INTO scintela.traza_detalle (componente, doc_id, etiqueta, importe, cantidad, precio)
SELECT componente, doc_id, etiqueta, importe, cantidad, precio
  FROM scintela.dia_detalle
ON CONFLICT (componente, doc_id) DO NOTHING;


-- ── De qué momento es cada foto ────────────────────────────────────────────
-- `origen` ya decía quién la disparó (loop | manual | test). `momento` dice
-- qué papel juega: una foto común, o el ancla de la mañana / del cierre.
ALTER TABLE scintela.traza_utilidad
    ADD COLUMN IF NOT EXISTS momento TEXT NOT NULL DEFAULT 'foto';


-- ── Los movimientos cuelgan de la foto, no de la captura del día ───────────
ALTER TABLE scintela.dia_movimiento
    ADD COLUMN IF NOT EXISTS id_traza BIGINT
        REFERENCES scintela.traza_utilidad (id_traza) ON DELETE CASCADE;

-- `id_captura` deja de ser obligatorio: de acá en adelante los movimientos
-- nacen de una foto de la traza. Las filas viejas (las que dejaron las
-- capturas del 05 y el 06 de agosto) conservan su `id_captura` y se siguen
-- leyendo por ahí — no se reescribe historia.
ALTER TABLE scintela.dia_movimiento
    ALTER COLUMN id_captura DROP NOT NULL;

CREATE INDEX IF NOT EXISTS ix_dia_movimiento_traza
    ON scintela.dia_movimiento (id_traza, componente);


-- ── El ancla del día apunta a su foto ──────────────────────────────────────
ALTER TABLE scintela.dia_captura
    ADD COLUMN IF NOT EXISTS id_traza BIGINT
        REFERENCES scintela.traza_utilidad (id_traza) ON DELETE SET NULL;


-- ── `dia_detalle` queda retirada ───────────────────────────────────────────
-- No se borra en esta migración a propósito: si algo sale mal con la cadencia
-- nueva, la foto vieja sigue ahí para volver atrás sin perder el punto de
-- partida. Se elimina en la limpieza de septiembre, junto con el código muerto
-- del dBase.
COMMENT ON TABLE scintela.dia_detalle IS
    'RETIRADA (mig 0171): la foto rodante vive en scintela.traza_detalle. Se conserva como punto de retorno; borrar en la limpieza de septiembre.';
