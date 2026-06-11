-- 0091_recompute_saldo_running_caja.sql
-- TMT 2026-06-11: recomputar el running balance (columna `saldo`) de
-- scintela.caja fila por fila, en orden (fecha ASC, id_caja ASC).
--
-- CAUSA RAÍZ del bug del 11/06: el trigger fn_caja_set_saldo (mig 0022)
-- sólo computa el saldo de la fila NUEVA. Si se inserta una fila
-- backdated (ej. el 10/06 se reversó un complemento de 0,99 y una
-- entrada de 2.460,00 y se recreó el movimiento real del dBase por
-- 2.460,99 con fecha vieja), las filas POSTERIORES quedan con el
-- running viejo → la última fila mostraba 17.862,97 cuando
-- inicial 80.053,99 + entradas 248.556,03 − salidas 308.286,06
-- = 20.323,96 (= dBase exacto). Diferencia: −2.460,99.
--
-- Qué hace: SOLO recalcula la columna `saldo`. NO toca fechas, tipos,
-- importes ni borra/crea filas. Convención de signo legacy:
-- E/CB/otros = +ABS(importe), S = −ABS(importe). Opening = saldo de la
-- primera fila con saldo NOT NULL menos su delta firmado (misma fórmula
-- que queries.saldo_actual()).
--
-- Idempotente: re-correrla deja los mismos valores (UPDATE sólo donde
-- IS DISTINCT FROM).

WITH base AS (
    SELECT id_caja,
           fecha,
           CASE WHEN UPPER(TRIM(COALESCE(tipo, ''))) = 'S'
                THEN -ABS(COALESCE(importe, 0))
                ELSE  ABS(COALESCE(importe, 0))
           END AS delta
      FROM scintela.caja
),
opening AS (
    SELECT COALESCE(
        (SELECT c.saldo
                - CASE WHEN UPPER(TRIM(COALESCE(c.tipo, ''))) = 'S'
                       THEN -ABS(COALESCE(c.importe, 0))
                       ELSE  ABS(COALESCE(c.importe, 0))
                  END
           FROM scintela.caja c
          WHERE c.saldo IS NOT NULL
          ORDER BY c.fecha ASC, c.id_caja ASC
          LIMIT 1),
        0) AS op
),
running AS (
    SELECT b.id_caja,
           ROUND(
               (SELECT op FROM opening)
               + SUM(b.delta) OVER (
                     ORDER BY b.fecha ASC, b.id_caja ASC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
               2) AS saldo_nuevo
      FROM base b
)
UPDATE scintela.caja c
   SET saldo = r.saldo_nuevo
  FROM running r
 WHERE r.id_caja = c.id_caja
   AND c.saldo IS DISTINCT FROM r.saldo_nuevo;
