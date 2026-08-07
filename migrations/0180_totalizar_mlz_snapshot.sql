-- 0180 — Reponer el snapshot del TOTALIZAR de MLZ (mov_doble #22176).
--
-- Contexto: el 07/08/2026 la dueña clickeó el ↺ del totalizar de MLZ y la app
-- contestó "Reverso no disponible desde acá. Tipo 'totalizar_estado_cuenta'
-- aún no tiene reverso automatizado". Era cierto: el totalizar nació
-- IRREVERSIBLE y no guardaba el estado previo. Este release lo hace
-- reversible (mov_doble.metadata.antes/despues/links), pero el mov #22176 ya
-- estaba escrito SIN snapshot.
--
-- Esta migración NO toca ninguna factura ni ningún cheque: sólo le agrega al
-- mov #22176 la metadata que le habría puesto el código nuevo, para que el
-- wizard de reverso pueda hacer su trabajo desde la pantalla. La decisión de
-- deshacerlo (o no) sigue siendo un click de la dueña en el Historial.
--
-- De dónde sale el `antes` (reconstruido, no inventado):
--   · FACTURAS.DBF del 03/08/2026 08:17 (última foto del dBase antes del
--     retiro) da abono/saldo/stat de 28 de las 29 facturas.
--   · La 180965 se emitió el 03/08 21:14 (posterior a esa foto): nació Z,
--     abono 0, saldo 1.642,65.
--   · El único movimiento posterior sobre estas facturas fue el cheque
--     #102028 aplicado a la 173937 el 07/08 18:25 (mov_doble #22175), un
--     minuto antes del totalizar: abono 2.870,36 → saldo −2.688,77, stat 'A'.
--   · No hubo NADA más entre esa foto y el totalizar (verificado sobre
--     mov_doble: sólo 3 factura_emitida y ese cheque).
--
-- Tres invariantes independientes cierran con lo que ya estaba guardado:
--   · Σ abono del `antes`  = 6.462,08 = el `pool` que el propio mov #22176
--     había registrado en su metadata.
--   · Σ saldo del `antes`  = 10.423,74 = Σ saldo de MLZ hoy (el totalizar
--     redistribuye, no crea ni borra deuda).
--   · Σ importe − pool     = 16.885,82 − 6.462,08 = 10.423,74.
--
-- El `despues` es la foto de las 29 facturas tal como las dejó el totalizar:
-- es el CANDADO del wizard (si alguna cambió después, no deja deshacer).
-- El `links` es el único vínculo cheque↔factura que el totalizar borró.

UPDATE scintela.mov_doble
   SET metadata = COALESCE(metadata, '{}'::jsonb) || '{
 "antes": [
  {
   "id": 273846,
   "numf": 169218,
   "importe": 1747.01,
   "abono": 0.0,
   "saldo": 1747.01,
   "stat": "Z"
  },
  {
   "id": 273870,
   "numf": 169693,
   "importe": 387.46,
   "abono": 0.0,
   "saldo": 387.46,
   "stat": "Z"
  },
  {
   "id": 273892,
   "numf": 169930,
   "importe": 991.33,
   "abono": 0.0,
   "saldo": 991.33,
   "stat": "Z"
  },
  {
   "id": 273932,
   "numf": 170384,
   "importe": 385.21,
   "abono": 0.0,
   "saldo": 385.21,
   "stat": "Z"
  },
  {
   "id": 273982,
   "numf": 170918,
   "importe": 190.24,
   "abono": 3591.72,
   "saldo": -3401.48,
   "stat": "A"
  },
  {
   "id": 274111,
   "numf": 171960,
   "importe": 191.14,
   "abono": 0.0,
   "saldo": 191.14,
   "stat": "Z"
  },
  {
   "id": 274265,
   "numf": 172822,
   "importe": 821.53,
   "abono": 0.0,
   "saldo": 821.53,
   "stat": "Z"
  },
  {
   "id": 274461,
   "numf": 173615,
   "importe": 1540.9,
   "abono": 0.0,
   "saldo": 1540.9,
   "stat": "Z"
  },
  {
   "id": 274521,
   "numf": 173836,
   "importe": 196.85,
   "abono": 0.0,
   "saldo": 196.85,
   "stat": "Z"
  },
  {
   "id": 274553,
   "numf": 173937,
   "importe": 181.59,
   "abono": 2870.36,
   "saldo": -2688.77,
   "stat": "A"
  },
  {
   "id": 274584,
   "numf": 174039,
   "importe": 1682.03,
   "abono": 0.0,
   "saldo": 1682.03,
   "stat": "Z"
  },
  {
   "id": 274837,
   "numf": 174762,
   "importe": 1841.0,
   "abono": 0.0,
   "saldo": 1841.0,
   "stat": "Z"
  },
  {
   "id": 274918,
   "numf": 174997,
   "importe": 862.6,
   "abono": 0.0,
   "saldo": 862.6,
   "stat": "Z"
  },
  {
   "id": 275028,
   "numf": 175215,
   "importe": 182.86,
   "abono": 0.0,
   "saldo": 182.86,
   "stat": "Z"
  },
  {
   "id": 275156,
   "numf": 10746,
   "importe": -346.59,
   "abono": 0.0,
   "saldo": -346.59,
   "stat": "Z"
  },
  {
   "id": 275297,
   "numf": 175728,
   "importe": 182.43,
   "abono": 0.0,
   "saldo": 182.43,
   "stat": "Z"
  },
  {
   "id": 275398,
   "numf": 175985,
   "importe": 676.71,
   "abono": 0.0,
   "saldo": 676.71,
   "stat": "Z"
  },
  {
   "id": 275451,
   "numf": 176078,
   "importe": 337.53,
   "abono": 0.0,
   "saldo": 337.53,
   "stat": "Z"
  },
  {
   "id": 275557,
   "numf": 176294,
   "importe": 171.48,
   "abono": 0.0,
   "saldo": 171.48,
   "stat": "Z"
  },
  {
   "id": 276503,
   "numf": 177484,
   "importe": 168.57,
   "abono": 0.0,
   "saldo": 168.57,
   "stat": "Z"
  },
  {
   "id": 276862,
   "numf": 177902,
   "importe": 1297.31,
   "abono": 0.0,
   "saldo": 1297.31,
   "stat": "Z"
  },
  {
   "id": 276949,
   "numf": 177992,
   "importe": 218.79,
   "abono": 0.0,
   "saldo": 218.79,
   "stat": "Z"
  },
  {
   "id": 277031,
   "numf": 178086,
   "importe": 175.67,
   "abono": 0.0,
   "saldo": 175.67,
   "stat": "Z"
  },
  {
   "id": 277248,
   "numf": 178319,
   "importe": 389.96,
   "abono": 0.0,
   "saldo": 389.96,
   "stat": "Z"
  },
  {
   "id": 277406,
   "numf": 11198,
   "importe": -145.85,
   "abono": 0.0,
   "saldo": -145.85,
   "stat": "Z"
  },
  {
   "id": 278021,
   "numf": 179058,
   "importe": 181.17,
   "abono": 0.0,
   "saldo": 181.17,
   "stat": "Z"
  },
  {
   "id": 278400,
   "numf": 179424,
   "importe": 496.22,
   "abono": 0.0,
   "saldo": 496.22,
   "stat": "Z"
  },
  {
   "id": 279233,
   "numf": 180276,
   "importe": 238.02,
   "abono": 0.0,
   "saldo": 238.02,
   "stat": "Z"
  },
  {
   "id": 280363,
   "numf": 180965,
   "importe": 1642.65,
   "abono": 0.0,
   "saldo": 1642.65,
   "stat": "Z"
  }
 ],
 "despues": [
  {
   "id": 273846,
   "abono": 1747.01,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 273870,
   "abono": 387.46,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 273892,
   "abono": 991.33,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 273932,
   "abono": 385.21,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 273982,
   "abono": 190.24,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274111,
   "abono": 191.14,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274265,
   "abono": 821.53,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274461,
   "abono": 1540.9,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274521,
   "abono": 196.85,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274553,
   "abono": 181.59,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 274584,
   "abono": 321.26,
   "saldo": 1360.77,
   "stat": "A"
  },
  {
   "id": 274837,
   "abono": 0.0,
   "saldo": 1841.0,
   "stat": "Z"
  },
  {
   "id": 274918,
   "abono": 0.0,
   "saldo": 862.6,
   "stat": "Z"
  },
  {
   "id": 275028,
   "abono": 0.0,
   "saldo": 182.86,
   "stat": "Z"
  },
  {
   "id": 275156,
   "abono": -346.59,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 275297,
   "abono": 0.0,
   "saldo": 182.43,
   "stat": "Z"
  },
  {
   "id": 275398,
   "abono": 0.0,
   "saldo": 676.71,
   "stat": "Z"
  },
  {
   "id": 275451,
   "abono": 0.0,
   "saldo": 337.53,
   "stat": "Z"
  },
  {
   "id": 275557,
   "abono": 0.0,
   "saldo": 171.48,
   "stat": "Z"
  },
  {
   "id": 276503,
   "abono": 0.0,
   "saldo": 168.57,
   "stat": "Z"
  },
  {
   "id": 276862,
   "abono": 0.0,
   "saldo": 1297.31,
   "stat": "Z"
  },
  {
   "id": 276949,
   "abono": 0.0,
   "saldo": 218.79,
   "stat": "Z"
  },
  {
   "id": 277031,
   "abono": 0.0,
   "saldo": 175.67,
   "stat": "Z"
  },
  {
   "id": 277248,
   "abono": 0.0,
   "saldo": 389.96,
   "stat": "Z"
  },
  {
   "id": 277406,
   "abono": -145.85,
   "saldo": 0.0,
   "stat": "T"
  },
  {
   "id": 278021,
   "abono": 0.0,
   "saldo": 181.17,
   "stat": "Z"
  },
  {
   "id": 278400,
   "abono": 0.0,
   "saldo": 496.22,
   "stat": "Z"
  },
  {
   "id": 279233,
   "abono": 0.0,
   "saldo": 238.02,
   "stat": "Z"
  },
  {
   "id": 280363,
   "abono": 0.0,
   "saldo": 1642.65,
   "stat": "Z"
  }
 ],
 "links": [
  {
   "id_cheque": 102028,
   "id_fact": 274553,
   "fechaing": "2026-08-07",
   "codigo_cli": "MLZ",
   "importe": 2870.36,
   "no_banco": 90,
   "tipo": null,
   "stat_f": "A",
   "fecha_venci_f": null,
   "abono_f": 2870.36,
   "saldo_f": -2688.77,
   "usuario_crea": "andres"
  }
 ]
}'::jsonb
 WHERE id_mov_doble = 22176
   AND tipo = 'totalizar_estado_cuenta'
   AND estado = 'activo'
   AND metadata->>'codigo_cli' = 'MLZ'
   AND metadata ? 'pool'
   AND (metadata->>'pool')::numeric = 6462.08
   AND NOT (metadata ? 'antes');
