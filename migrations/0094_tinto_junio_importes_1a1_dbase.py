"""0094 — TINTO junio: importes 1 a 1 contra TINTO.DBF (tarball 2026-06-11).

Causa del drift VQX +12.465,71 (PC - dBase): el dBase recalculo/retipeo los
IMPORTES de TINTO (colorantes) y la planilla pc-carga del 10/06 uso los
costos del catalogo scintela.tinto_costos, que NO son los que el dBase
aplica hoy. Los kg/kgn coinciden EXACTO codigo por codigo — solo difiere
el importe. Ademas faltan 4 filas LAV (lavado de maquina, kg=0) del 10/06.

Regla duena 2026-06-11: cargar SOLO lo del dBase, 1 a 1, nunca bulk.
Este script:
  [1] aparea cada fila PC de junio con una fila del DBF por la clave
      exacta (fecha, cod, kg, kgn) — multiset, asignacion unica;
  [2] en cada par, si el importe difiere -> UPDATE a ese importe del DBF
      (fila por fila, por id_tinto);
  [3] filas del DBF que quedaron SIN par y no chocan con ninguna fila PC
      sin par del mismo (fecha, cod) -> INSERT (son las 4 LAV del 10/06);
      la fila vacia cod='' todo-cero del DBF se saltea;
  [4] filas PC sin par o casos ambiguos -> NO SE TOCAN, se reportan.

Esperado post-mig: ITIN junio PC = 89.692,00 = dBase; VQX delta 0,00.
Idempotente: re-correr re-aparea y todos los updates son no-op.
"""

DBF = [('2026-06-01', 'LAV', 'LAVADO MAQ', 0.0, 1500.0, 956.0, 'L'), ('2026-06-01', 'JOS', 'JAS.OSCURO', 507.0, 483.0, 38.0, 'Z'), ('2026-06-01', 'BLA', 'BLANCO', 1154.0, 1090.0, 176.0, 'Z'), ('2026-06-01', 'BHU', 'B.HUESO', 783.0, 737.0, 79.0, 'Z'), ('2026-06-01', 'SEL', 'SELECCION', 381.0, 361.0, 280.0, 'Z'), ('2026-06-01', 'AME', 'A.ECUADOR', 722.0, 695.0, 1160.0, 'Z'), ('2026-06-01', 'VPI', 'V.PINO', 271.0, 263.0, 711.0, 'Z'), ('2026-06-01', 'ROJ', 'ROJO', 360.0, 348.0, 477.0, 'Z'), ('2026-06-01', 'AJM', 'A.J.MEDIO', 321.0, 305.0, 467.0, 'Z'), ('2026-06-01', 'UVJ', 'UVA JASPEA', 215.0, 205.0, 78.0, 'Z'), ('2026-06-01', 'RBB', 'ROSADO BB', 180.0, 174.0, 28.0, 'Z'), ('2026-06-01', 'PAT', 'AM.PATO', 302.0, 288.0, 60.0, 'Z'), ('2026-06-01', 'VAG', 'V.AGUA', 179.0, 174.0, 53.0, 'Z'), ('2026-06-01', 'VPS', 'V.PISTACHO', 501.0, 473.0, 154.0, 'Z'), ('2026-06-01', 'PLO', 'PLOMO', 771.0, 733.0, 274.0, 'Z'), ('2026-06-01', 'CHI', 'CHICLE', 285.0, 275.0, 281.0, 'Z'), ('2026-06-01', 'UVA', 'UVA', 190.0, 180.0, 64.0, 'Z'), ('2026-06-01', 'TMT', 'TOMATE', 297.0, 285.0, 287.0, 'Z'), ('2026-06-01', 'FRE', 'FRESA', 162.0, 154.0, 269.0, 'Z'), ('2026-06-01', 'UVM', 'UVA MORA', 216.0, 205.0, 60.0, 'Z'), ('2026-06-01', 'VIO', 'VIOLETA', 135.0, 131.0, 223.0, 'Z'), ('2026-06-01', 'MOD', 'MORADO', 148.0, 142.0, 69.0, 'Z'), ('2026-06-01', 'VFP', 'VINO FP', 190.0, 181.0, 219.0, 'Z'), ('2026-06-01', 'MZA', 'V.MANZANA', 295.0, 275.0, 1014.0, 'Z'), ('2026-06-01', 'CAR', 'CARDENILLO', 681.0, 650.0, 2309.0, 'Z'), ('2026-06-01', 'CIE', 'CIELO', 319.0, 301.0, 456.0, 'Z'), ('2026-06-01', 'ELE', 'ELECTRICO', 997.0, 956.0, 2924.0, 'Z'), ('2026-06-01', 'EFP', 'ELECT. FP', 540.0, 522.0, 2603.0, 'Z'), ('2026-06-01', 'BOT', 'V.BOTELLA', 1218.0, 1166.0, 1648.0, 'Z'), ('2026-06-01', 'TOP', 'TOPACIO', 137.0, 130.0, 86.0, 'Z'), ('2026-06-01', 'ROY', 'ROYAL', 271.0, 262.0, 359.0, 'Z'), ('2026-06-01', 'REY', 'AZUL REY', 477.0, 452.0, 487.0, 'Z'), ('2026-06-01', 'PET', 'PETROLEO', 863.0, 825.0, 1166.0, 'Z'), ('2026-06-01', 'MAR', 'MARINO', 930.0, 915.0, 1279.0, 'Z'), ('2026-06-01', 'AZN', 'AZ.NOCHE', 2920.0, 2862.0, 5138.0, 'Z'), ('2026-06-01', 'NEG', 'NEGRO', 3769.0, 3657.0, 5478.0, 'Z'), ('2026-06-03', 'LAV', 'LAVADO MAQ', 0.0, 300.0, 138.0, 'L'), ('2026-06-03', 'JAS', 'JASPEADO', 998.0, 933.0, 37.0, 'Z'), ('2026-06-03', 'JME', 'JAS MEDIO', 1574.0, 1491.0, 403.0, 'Z'), ('2026-06-03', 'JOS', 'JAS.OSCURO', 530.0, 500.0, 30.0, 'Z'), ('2026-06-03', 'JFP', 'JAS OSC FP', 521.0, 499.0, 47.0, 'Z'), ('2026-06-03', 'BLA', 'BLANCO', 2967.0, 2845.0, 325.0, 'Z'), ('2026-06-03', 'BHU', 'B.HUESO', 354.0, 334.0, 25.0, 'Z'), ('2026-06-03', 'BMT', 'B.MATE', 382.0, 361.0, 126.0, 'Z'), ('2026-06-03', 'HAB', 'HABANO', 450.0, 424.0, 39.0, 'Z'), ('2026-06-03', 'SEL', 'SELECCION', 321.0, 304.0, 170.0, 'Z'), ('2026-06-03', 'FRE', 'FRESA', 1026.0, 989.0, 1223.0, 'Z'), ('2026-06-03', 'AVE', 'AVELLANA', 136.0, 129.0, 42.0, 'Z'), ('2026-06-03', 'MZA', 'V.MANZANA', 304.0, 290.0, 751.0, 'Z'), ('2026-06-03', 'GCL', 'GRIS CLARO', 526.0, 502.0, 174.0, 'Z'), ('2026-06-03', 'VPE', 'V.PETROLEO', 408.0, 390.0, 516.0, 'Z'), ('2026-06-03', 'PET', 'PETROLEO', 497.0, 480.0, 482.0, 'Z'), ('2026-06-03', 'MAR', 'MARINO', 515.0, 504.0, 509.0, 'Z'), ('2026-06-03', 'NOS', 'NEGRO OSCU', 498.0, 487.0, 1186.0, 'Z'), ('2026-06-03', 'NEG', 'NEGRO', 2486.0, 2407.0, 2595.0, 'Z'), ('2026-06-03', 'JOS', 'JAS.OSCURO', 444.0, 431.0, 23.0, 'Z'), ('2026-06-03', 'BLA', 'BLANCO', 1805.0, 1721.0, 198.0, 'Z'), ('2026-06-03', 'AME', 'A.ECUADOR', 360.0, 347.0, 417.0, 'Z'), ('2026-06-03', 'VHE', 'V.HELECHO', 147.0, 142.0, 258.0, 'Z'), ('2026-06-03', 'CPU', 'CAPUCHINO', 452.0, 429.0, 67.0, 'Z'), ('2026-06-03', 'PER', 'PERICO', 332.0, 320.0, 535.0, 'Z'), ('2026-06-03', 'RCJ', 'CARMIN JAS', 318.0, 301.0, 389.0, 'Z'), ('2026-06-03', 'VFP', 'VINO FP', 301.0, 292.0, 248.0, 'Z'), ('2026-06-03', 'BOT', 'V.BOTELLA', 329.0, 318.0, 318.0, 'Z'), ('2026-06-03', 'ACE', 'ACERO', 329.0, 316.0, 132.0, 'Z'), ('2026-06-03', 'PET', 'PETROLEO', 775.0, 745.0, 753.0, 'Z'), ('2026-06-03', 'ACU', 'ACUSTICO', 372.0, 359.0, 246.0, 'Z'), ('None', '', '', 0.0, 0.0, 0.0, ''), ('2026-06-03', 'MAR', 'MARINO', 920.0, 891.0, 911.0, 'Z'), ('2026-06-03', 'AZN', 'AZ.NOCHE', 1389.0, 1344.0, 1755.0, 'Z'), ('2026-06-03', 'NEG', 'NEGRO', 543.0, 523.0, 567.0, 'Z'), ('2026-06-05', 'LAV', 'LAVADO MAQ', 0.0, 600.0, 222.0, 'L'), ('2026-06-05', 'JFP', 'JAS OSC FP', 135.0, 130.0, 10.0, 'Z'), ('2026-06-05', 'BLA', 'BLANCO', 2730.0, 2594.0, 243.0, 'Z'), ('2026-06-05', 'BAZ', 'B.AZULADO', 466.0, 449.0, 49.0, 'Z'), ('2026-06-05', 'SEL', 'SELECCION', 268.0, 256.0, 116.0, 'Z'), ('2026-06-05', 'MFL', 'MARFIL', 137.0, 131.0, 116.0, 'Z'), ('2026-06-05', 'CRU', 'CRUDO', 902.0, 860.0, 80.0, 'Z'), ('2026-06-05', 'MAN', 'MANTECA', 271.0, 257.0, 28.0, 'Z'), ('2026-06-05', 'PAT', 'AM.PATO', 308.0, 288.0, 37.0, 'Z'), ('2026-06-05', 'ARE', 'ARENA', 192.0, 182.0, 15.0, 'Z'), ('2026-06-05', 'CEL', 'CELESTE', 465.0, 447.0, 152.0, 'Z'), ('2026-06-05', 'NAV', 'NAVAL', 287.0, 274.0, 38.0, 'Z'), ('2026-06-05', 'PIE', 'AZ.PIEDRA', 149.0, 142.0, 49.0, 'Z'), ('2026-06-05', 'BRI', 'BRILLANTE', 285.0, 274.0, 134.0, 'Z'), ('2026-06-05', 'BAN', 'BANDERA', 483.0, 463.0, 115.0, 'Z'), ('2026-06-05', 'MOS', 'MOSTAZA', 284.0, 272.0, 71.0, 'Z'), ('2026-06-05', 'CPU', 'CAPUCHINO', 1101.0, 1038.0, 131.0, 'Z'), ('2026-06-05', 'NAR', 'NARANJA', 480.0, 465.0, 157.0, 'Z'), ('2026-06-05', 'PER', 'PERICO', 614.0, 590.0, 800.0, 'Z'), ('2026-06-05', 'MZA', 'V.MANZANA', 305.0, 296.0, 610.0, 'Z'), ('2026-06-05', 'CAF', 'CAFE', 317.0, 308.0, 292.0, 'Z'), ('2026-06-05', 'CIE', 'CIELO', 627.0, 590.0, 520.0, 'Z'), ('2026-06-05', 'ELE', 'ELECTRICO', 998.0, 971.0, 1701.0, 'Z'), ('2026-06-05', 'CEN', 'CENIZA', 272.0, 261.0, 161.0, 'Z'), ('2026-06-05', 'GRI', 'GRIS', 318.0, 308.0, 169.0, 'Z'), ('2026-06-05', 'PET', 'PETROLEO', 376.0, 364.0, 295.0, 'Z'), ('2026-06-05', 'JNE', 'JASP.NEGRO', 313.0, 307.0, 181.0, 'Z'), ('2026-06-05', 'MAR', 'MARINO', 2334.0, 2267.0, 1866.0, 'Z'), ('2026-06-05', 'AZN', 'AZ.NOCHE', 3506.0, 3420.0, 3583.0, 'Z'), ('2026-06-05', 'NEG', 'NEGRO', 1451.0, 1428.0, 1225.0, 'Z'), ('2026-06-10', 'LAV', 'LAVADO MAQ', 0.0, 600.0, 185.0, 'L'), ('2026-06-10', 'LAV', 'LAVADO MAQ', 0.0, 600.0, 185.0, 'L'), ('2026-06-10', 'JAS', 'JASPEADO', 128.0, 122.0, 4.0, 'Z'), ('2026-06-10', 'JME', 'JAS MEDIO', 374.0, 356.0, 64.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 447.0, 421.0, 16.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 497.0, 470.0, 18.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 454.0, 427.0, 17.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 445.0, 421.0, 16.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 454.0, 428.0, 17.0, 'Z'), ('2026-06-10', 'BAZ', 'B.AZULADO', 493.0, 472.0, 43.0, 'Z'), ('2026-06-10', 'CRU', 'CRUDO', 270.0, 256.0, 20.0, 'Z'), ('2026-06-10', 'MAN', 'MANTECA', 271.0, 259.0, 23.0, 'Z'), ('2026-06-10', 'ROS', 'ROSADO', 136.0, 128.0, 15.0, 'Z'), ('2026-06-10', 'HAB', 'HABANO', 136.0, 128.0, 9.0, 'Z'), ('2026-06-10', 'HAB', 'HABANO', 360.0, 341.0, 22.0, 'Z'), ('2026-06-10', 'CEL', 'CELESTE', 145.0, 139.0, 39.0, 'Z'), ('2026-06-10', 'CEL', 'CELESTE', 453.0, 436.0, 123.0, 'Z'), ('2026-06-10', 'NAV', 'NAVAL', 136.0, 129.0, 15.0, 'Z'), ('2026-06-10', 'NAV', 'NAVAL', 496.0, 480.0, 55.0, 'Z'), ('2026-06-10', 'NAV', 'NAVAL', 294.0, 278.0, 32.0, 'Z'), ('2026-06-10', 'PLO', 'PLOMO', 157.0, 143.0, 27.0, 'Z'), ('2026-06-10', 'PLO', 'PLOMO', 302.0, 282.0, 52.0, 'Z'), ('2026-06-10', 'AME', 'A.ECUADOR', 269.0, 259.0, 208.0, 'Z'), ('2026-06-10', 'MAI', 'MAIZ', 309.0, 294.0, 35.0, 'Z'), ('2026-06-10', 'CPU', 'CAPUCHINO', 452.0, 429.0, 44.0, 'Z'), ('2026-06-10', 'AVE', 'AVELLANA', 296.0, 280.0, 62.0, 'Z'), ('2026-06-10', 'FRE', 'FRESA', 352.0, 337.0, 282.0, 'Z'), ('2026-06-10', 'RCA', 'R.CARMIN', 192.0, 185.0, 187.0, 'Z'), ('2026-06-10', 'VTI', 'V.TINTO', 400.0, 384.0, 276.0, 'Z'), ('2026-06-10', 'VIN', 'VINO', 528.0, 506.0, 312.0, 'Z'), ('2026-06-10', 'CAF', 'CAFE', 166.0, 159.0, 127.0, 'Z'), ('2026-06-10', 'PER', 'PERICO', 333.0, 319.0, 361.0, 'Z'), ('2026-06-10', 'PER', 'PERICO', 333.0, 317.0, 361.0, 'Z'), ('2026-06-10', 'CAR', 'CARDENILLO', 285.0, 277.0, 467.0, 'Z'), ('2026-06-10', 'CAR', 'CARDENILLO', 226.0, 216.0, 371.0, 'Z'), ('2026-06-10', 'TOP', 'TOPACIO', 309.0, 294.0, 95.0, 'Z'), ('2026-06-10', 'CEN', 'CENIZA', 488.0, 468.0, 240.0, 'Z'), ('2026-06-10', 'NOS', 'NEGRO OSCU', 164.0, 156.0, 262.0, 'Z'), ('2026-06-10', 'NEG', 'NEGRO', 317.0, 310.0, 223.0, 'Z'), ('2026-06-10', 'LAV', 'LAVADO MAQ', 0.0, 600.0, 185.0, 'L'), ('2026-06-10', 'RBB', 'ROSADO BB', 230.0, 218.0, 17.0, 'Z'), ('2026-06-10', 'HAB', 'HABANO', 533.0, 510.0, 33.0, 'Z'), ('2026-06-10', 'AVB', 'AVANDA BB', 187.0, 180.0, 30.0, 'Z'), ('2026-06-10', 'CEL', 'CELESTE', 752.0, 717.0, 203.0, 'Z'), ('2026-06-10', 'SEL', 'SELECCION', 567.0, 543.0, 202.0, 'Z'), ('2026-06-10', 'BAN', 'BANDERA', 438.0, 421.0, 86.0, 'Z'), ('2026-06-10', 'CPU', 'CAPUCHINO', 136.0, 126.0, 14.0, 'Z'), ('2026-06-10', 'MVA', 'MALVA', 192.0, 182.0, 26.0, 'Z'), ('2026-06-10', 'FRE', 'FRESA', 452.0, 436.0, 362.0, 'Z'), ('2026-06-10', 'VIN', 'VINO', 166.0, 159.0, 99.0, 'Z'), ('2026-06-10', 'CAF', 'CAFE', 386.0, 369.0, 295.0, 'Z'), ('2026-06-10', 'VHE', 'V.HELECHO', 358.0, 336.0, 424.0, 'Z'), ('2026-06-10', 'CIE', 'CIELO', 635.0, 605.0, 439.0, 'Z'), ('2026-06-10', 'OLI', 'V.OLIVA', 738.0, 704.0, 455.0, 'Z'), ('2026-06-10', 'VPI', 'V.PINO', 196.0, 191.0, 249.0, 'Z'), ('2026-06-10', 'CRO', 'CROMO', 282.0, 267.0, 157.0, 'Z'), ('2026-06-10', 'CEN', 'CENIZA', 271.0, 258.0, 133.0, 'Z'), ('2026-06-10', 'GRI', 'GRIS', 322.0, 311.0, 143.0, 'Z'), ('2026-06-10', 'MAR', 'MARINO', 2350.0, 2270.0, 1564.0, 'Z'), ('2026-06-10', 'AZN', 'AZ.NOCHE', 525.0, 510.0, 446.0, 'Z'), ('2026-06-10', 'NEG', 'NEGRO', 1272.0, 1253.0, 893.0, 'Z'), ('2026-06-10', 'LAV', 'LAVADO MAQ', 0.0, 1800.0, 555.0, 'L'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 2736.0, 2566.0, 101.0, 'Z'), ('2026-06-10', 'BLA', 'BLANCO', 1835.0, 1737.0, 136.0, 'Z'), ('2026-06-10', 'BHU', 'B.HUESO', 404.0, 384.0, 20.0, 'Z'), ('2026-06-10', 'BMT', 'B.MATE', 270.0, 248.0, 60.0, 'Z'), ('2026-06-10', 'BAZ', 'B.AZULADO', 500.0, 478.0, 43.0, 'Z'), ('2026-06-10', 'CRU', 'CRUDO', 627.0, 585.0, 47.0, 'Z'), ('2026-06-10', 'PLO', 'PLOMO', 503.0, 471.0, 86.0, 'Z'), ('2026-06-10', 'HAB', 'HABANO', 166.0, 156.0, 10.0, 'Z'), ('2026-06-10', 'NAV', 'NAVAL', 165.0, 157.0, 18.0, 'Z'), ('2026-06-10', 'RSA', 'ROSA', 472.0, 448.0, 41.0, 'Z'), ('2026-06-10', 'SEL', 'SELECCION', 425.0, 403.0, 152.0, 'Z'), ('2026-06-10', 'BAN', 'BANDERA', 272.0, 259.0, 54.0, 'Z'), ('2026-06-10', 'PAP', 'PAPAYA', 321.0, 306.0, 55.0, 'Z'), ('2026-06-10', 'LIM', 'LILA MEDIO', 165.0, 155.0, 32.0, 'Z'), ('2026-06-10', 'CPP', 'CP POLICIA', 451.0, 432.0, 44.0, 'Z'), ('2026-06-10', 'NAR', 'NARANJA', 135.0, 129.0, 37.0, 'Z'), ('2026-06-10', 'VPS', 'V.PISTACHO', 164.0, 156.0, 25.0, 'Z'), ('2026-06-10', 'CPU', 'CAPUCHINO', 436.0, 413.0, 43.0, 'Z'), ('2026-06-10', 'ROJ', 'ROJO', 300.0, 285.0, 192.0, 'Z'), ('2026-06-10', 'FRE', 'FRESA', 502.0, 482.0, 402.0, 'Z'), ('2026-06-10', 'AVE', 'AVELLANA', 128.0, 123.0, 27.0, 'Z'), ('2026-06-10', 'ROV', 'ROSA VIEJO', 166.0, 157.0, 21.0, 'Z'), ('2026-06-10', 'RCA', 'R.CARMIN', 308.0, 296.0, 299.0, 'Z'), ('2026-06-10', 'VFP', 'VINO FP', 282.0, 271.0, 157.0, 'Z'), ('2026-06-10', 'VIN', 'VINO', 499.0, 478.0, 296.0, 'Z'), ('2026-06-10', 'TAB', 'TABACO', 272.0, 269.0, 150.0, 'Z'), ('2026-06-10', 'VTQ', 'V.TURQUEZA', 178.0, 171.0, 261.0, 'Z'), ('2026-06-10', 'EFP', 'ELECT. FP', 271.0, 259.0, 631.0, 'Z'), ('2026-06-10', 'ELE', 'ELECTRICO', 310.0, 296.0, 439.0, 'Z'), ('2026-06-10', 'ACT', 'ACEITUNA', 135.0, 123.0, 62.0, 'Z'), ('2026-06-10', 'BOT', 'V.BOTELLA', 436.0, 422.0, 285.0, 'Z'), ('2026-06-10', 'ROY', 'ROYAL', 444.0, 420.0, 285.0, 'Z'), ('2026-06-10', 'GRV', 'GRAVA', 135.0, 124.0, 33.0, 'Z'), ('2026-06-10', 'GCL', 'GRIS CLARO', 196.0, 188.0, 43.0, 'Z'), ('2026-06-10', 'CEN', 'CENIZA', 273.0, 264.0, 134.0, 'Z'), ('2026-06-10', 'ACU', 'ACUSTICO', 775.0, 752.0, 344.0, 'Z'), ('2026-06-10', 'PET', 'PETROLEO', 287.0, 277.0, 187.0, 'Z'), ('2026-06-10', 'POS', 'PLOMO OSC.', 321.0, 305.0, 150.0, 'Z'), ('2026-06-10', 'AZM', 'AZUL MEDIO', 277.0, 270.0, 235.0, 'Z'), ('2026-06-10', 'MAR', 'MARINO', 1263.0, 1241.0, 840.0, 'Z'), ('2026-06-10', 'AZN', 'AZ.NOCHE', 1477.0, 1419.0, 1256.0, 'Z'), ('2026-06-10', 'NOS', 'NEGRO OSCU', 860.0, 847.0, 1378.0, 'Z'), ('2026-06-10', 'NEG', 'NEGRO', 1394.0, 1341.0, 980.0, 'Z'), ('2026-06-10', 'JOS', 'JAS.OSCURO', 1815.0, 1731.0, 67.0, 'Z'), ('2026-06-10', 'BLA', 'BLANCO', 6968.0, 6638.0, 515.0, 'Z'), ('2026-06-10', 'AME', 'A.ECUADOR', 652.0, 631.0, 507.0, 'Z'), ('2026-06-10', 'CRU', 'CRUDO', 1149.0, 1093.0, 85.0, 'Z'), ('2026-06-10', 'PLO', 'PLOMO', 347.0, 331.0, 60.0, 'Z'), ('2026-06-10', 'CEL', 'CELESTE', 332.0, 317.0, 90.0, 'Z'), ('2026-06-10', 'ARV', 'ARVEJA', 316.0, 302.0, 70.0, 'Z'), ('2026-06-10', 'NAR', 'NARANJA', 300.0, 289.0, 81.0, 'Z'), ('2026-06-10', 'ROJ', 'ROJO', 159.0, 153.0, 102.0, 'Z'), ('2026-06-10', 'FRE', 'FRESA', 692.0, 664.0, 555.0, 'Z'), ('2026-06-10', 'VIO', 'VIOLETA', 159.0, 153.0, 127.0, 'Z'), ('2026-06-10', 'CIR', 'CIRUELA', 306.0, 300.0, 177.0, 'Z'), ('2026-06-10', 'VTI', 'V.TINTO', 156.0, 151.0, 107.0, 'Z'), ('2026-06-10', 'AVE', 'AVELLANA', 688.0, 651.0, 144.0, 'Z'), ('2026-06-10', 'CAF', 'CAFE', 318.0, 303.0, 243.0, 'Z'), ('2026-06-10', 'CEM', 'CEMENTO', 147.0, 140.0, 46.0, 'Z'), ('2026-06-10', 'OLI', 'V.OLIVA', 298.0, 287.0, 184.0, 'Z'), ('2026-06-10', 'VPI', 'V.PINO', 499.0, 475.0, 633.0, 'Z'), ('2026-06-10', 'BOT', 'V.BOTELLA', 522.0, 507.0, 341.0, 'Z'), ('2026-06-10', 'REY', 'AZUL REY', 310.0, 299.0, 153.0, 'Z'), ('2026-06-10', 'ACU', 'ACUSTICO', 395.0, 383.0, 175.0, 'Z'), ('2026-06-10', 'PET', 'PETROLEO', 166.0, 160.0, 108.0, 'Z'), ('2026-06-10', 'FAR', 'FARFAN', 523.0, 502.0, 264.0, 'Z'), ('2026-06-10', 'MAR', 'MARINO', 1256.0, 1198.0, 836.0, 'Z'), ('2026-06-10', 'AZN', 'AZ.NOCHE', 516.0, 500.0, 439.0, 'Z'), ('2026-06-10', 'NEG', 'NEGRO', 1936.0, 1879.0, 1361.0, 'Z')]


def run(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id_tinto, fecha::text, UPPER(TRIM(COALESCE(cod,''))) AS cod,
               ROUND(COALESCE(kg,0)::numeric, 2)  AS kg,
               ROUND(COALESCE(kgn,0)::numeric, 2) AS kgn,
               ROUND(COALESCE(importe,0)::numeric, 2) AS importe,
               COALESCE(usuario_crea,'') AS usuario
          FROM scintela.tinto
         WHERE fecha >= DATE '2026-06-01' AND fecha < DATE '2026-07-01'
        """
    )
    pc = [{"id": r[0], "fecha": r[1], "cod": r[2], "kg": float(r[3]),
           "kgn": float(r[4]), "importe": float(r[5]), "u": r[6]}
          for r in cur.fetchall()]

    # Guard anti-entorno-equivocado: este fix es para la base PROD que tiene
    # la planilla pc-carga de junio. En una DB de test/vacia (CI aplica todas
    # las migraciones) no hay nada que aparear -> no-op total, sin inserts.
    if not any(r["u"] == "pc-carga" for r in pc):
        print("  0094 no-op: no hay filas pc-carga de junio en esta DB "
              "(entorno test o planilla ya absorbida por sync)")
        return

    pend = {}
    for fecha, cod, color, kg, kgn, imp, stat in DBF:
        pend.setdefault((fecha, cod, kg, kgn), []).append(
            {"fecha": fecha, "cod": cod, "color": color, "kg": kg,
             "kgn": kgn, "importe": imp, "stat": stat})

    upd = nop = 0
    pc_sin_par = []
    for row in pc:
        k = (row["fecha"], row["cod"], row["kg"], row["kgn"])
        lst = pend.get(k)
        if not lst:
            pc_sin_par.append(row)
            continue
        dbf_row = lst.pop(0)
        if not lst:
            del pend[k]
        if abs(dbf_row["importe"] - row["importe"]) > 0.005:
            cur.execute(
                "UPDATE scintela.tinto SET importe = %s, "
                "usuario_modifica = 'mig-0094', fecha_modifica = CURRENT_TIMESTAMP "
                "WHERE id_tinto = %s",
                (dbf_row["importe"], row["id"]),
            )
            print(f"  UPDATE id={row['id']} {row['fecha']} {row['cod']:5} kg={row['kg']:>9,.2f} "
                  f"importe {row['importe']:>9,.2f} -> {dbf_row['importe']:>9,.2f} (u={row['u']})")
            upd += 1
        else:
            nop += 1

    leftover = [r for lst in pend.values() for r in lst]
    pc_keys_libres = {(r["fecha"], r["cod"]) for r in pc_sin_par}
    ins = 0
    omitidas = []
    for r in leftover:
        if r["cod"] == "" and r["kg"] == 0 and r["kgn"] == 0 and r["importe"] == 0:
            omitidas.append((r, "fila vacia del DBF"))
            continue
        if (r["fecha"], r["cod"]) in pc_keys_libres:
            omitidas.append((r, "AMBIGUA: hay fila PC sin par con mismo (fecha,cod) — no se toca"))
            continue
        cur.execute(
            """
            INSERT INTO scintela.tinto (fecha, cod, color, kg, kgn, importe, stat, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'dbf-import')
            """,
            (r["fecha"], r["cod"], r["color"][:30], r["kg"], r["kgn"],
             r["importe"], r["stat"][:1]),
        )
        print(f"  INSERT {r['fecha']} {r['cod']:5} {r['color'][:14]:14} kgn={r['kgn']:>8,.2f} "
              f"importe={r['importe']:>8,.2f} stat={r['stat']!r}")
        ins += 1

    for r in pc_sin_par:
        print(f"  PC SIN PAR (no se toca): id={r['id']} {r['fecha']} {r['cod']:5} "
              f"kg={r['kg']:>9,.2f} kgn={r['kgn']:>9,.2f} importe={r['importe']:>9,.2f} u={r['u']}")
    for r, motivo in omitidas:
        print(f"  DBF OMITIDA ({motivo}): {r['fecha']} {r['cod']!r} kgn={r['kgn']} imp={r['importe']}")

    cur.execute(
        "SELECT ROUND(COALESCE(SUM(importe),0)::numeric,2), COUNT(*) FROM scintela.tinto "
        "WHERE fecha >= DATE '2026-06-01' AND fecha < DATE '2026-07-01'"
    )
    itin, n = cur.fetchone()
    print(f"  RESUMEN: {upd} updates, {nop} ya-iguales, {ins} inserts, "
          f"{len(pc_sin_par)} PC sin par, {len(omitidas)} DBF omitidas")
    print(f"  ITIN junio post-mig = {itin} ({n} filas) — esperado 89.692,00 (232+pc extras)")
