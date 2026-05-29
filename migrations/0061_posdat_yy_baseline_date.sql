-- Migration 0061: baseline_date en scintela.posdat para YY display-time.
--
-- Contexto (TMT 2026-05-28): hasta esta migración, el importe de las posdat
-- prov='YY' se incrementaba con un job (`correr_provisiones_diarias`, en
-- `modules/informes/queries.py`) que corría lazy al cargar /informes/balance.
-- El problema: depende de que alguien entre cada día hábil para que el cron
-- aplique, y la columna `importe` muta day-by-day, sin marca de "qué día se
-- aplicó", lo cual hace difícil debuggear cuando un valor sale mal.
--
-- Cambio: el importe pasa a calcularse DISPLAY-TIME a partir de
--   importe_persistido + cuota_diaria × dias_habiles(baseline_date, hoy)
-- donde `cuota_diaria` viene de `scintela.provisiones` (matched por concepto)
-- o se deriva del posdat mismo (caso RT).
--
-- Lógica de mes:
--   - Mes en curso: importe_persistido queda fijo; el offset crece L-V.
--   - Al cruzar mes (lazy en primer hit del mes nuevo): se registra
--     mov_doble tipo='posdat_yy_cierre_mes' con el valor final del mes
--     anterior, se RESETEA importe_persistido a 0 y baseline_date al
--     último día calendario del mes anterior (31/05 después de mayo).
--     Esto hace que el primer día hábil del mes nuevo cuente como
--     offset=1 y ya muestre cuota_diaria.
--
-- Esta migración:
--   1. Agrega columna `baseline_date DATE` (NULL para no-YY y legacy).
--   2. Backfilea `baseline_date = CURRENT_DATE` para todas las YY abiertas
--      (banc=0 y no anuladas). Snapshot HOY: lo que muestra la app ahora
--      queda como base; el incremento empieza mañana.
--   3. Avanza el marker del cron `provisiones_diarias_ult_fecha` a HOY
--      para que NO aplique más incrementos sobre estas filas con baseline.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS + UPDATE filtra por baseline IS NULL.

ALTER TABLE scintela.posdat
    ADD COLUMN IF NOT EXISTS baseline_date DATE;

-- Backfill snapshot HOY para todas las YY abiertas que aún no tengan baseline.
-- Las cerradas (banc=9) y anuladas no se tocan — no aplica el display-time.
UPDATE scintela.posdat
   SET baseline_date = CURRENT_DATE
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL)
   AND baseline_date IS NULL;

-- Neutralizar el cron: avanza el marker a HOY para que el próximo tick
-- no aplique ningún día sobre filas que ahora tienen baseline.
-- (El cron también filtra `baseline_date IS NULL` en su UPDATE, así que
--  esto es defensa-en-profundidad — si se crea una posdat YY sin baseline
--  el cron la sigue actualizando como antes.)
INSERT INTO scintela.sistema_meta (clave, valor)
VALUES ('provisiones_diarias_ult_fecha', CURRENT_DATE::text)
ON CONFLICT (clave) DO UPDATE
   SET valor = EXCLUDED.valor,
       actualizado = CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_posdat_yy_baseline
    ON scintela.posdat (baseline_date)
    WHERE UPPER(COALESCE(prov, '')) = 'YY'
      AND baseline_date IS NOT NULL;

COMMENT ON COLUMN scintela.posdat.baseline_date IS
    'YY-only: fecha desde la cual cuota_diaria se acumula display-time. '
    'NULL en posdat no-YY y en YY legacy pre-migración 0061. Cuando se '
    'cruza un mes, el código lazy resetea importe=0 y avanza baseline_date '
    'al último día calendario del mes anterior, dejando registro del cierre '
    'en mov_doble tipo=''posdat_yy_cierre_mes''.';
