-- 0251 · Después de la largada sólo entra segunda
--
-- Dueña 18/09/2026, mirando /analisis/entradas: *"de la tela parada no
-- debería entrar nueva, solo de segunda"*.
--
-- "La cohorte sólo crece" dejaba entrar en cada refresco cualquier tela ×
-- color que ESE día cumpliera la regla de parada (12 meses sin venta, 90 días
-- en bodega, sin pedido ni orden). La que cruza esa línea después de la
-- largada (25/08) no es un saldo que la competencia vino a destrabar: es tela
-- que se estancó DURANTE la carrera. La segunda entra igual, siempre.
--
-- El código (queries.actualizar) ya no las inserta ni vuelve a encender las
-- apagadas. ACÁ se apagan las que ya habían entrado. Medido el 18/09/2026:
-- 5 ítems, 438 kg, ninguno vendido —apagarlos no le saca un punto a nadie—:
--
--   08/09  Asturias CAR 99 · Asturias ELE 39
--   09/09  Fleece 96 Perchado COJ 24
--   10/09  Toper ACM 206
--   15/09  Microfibra 1.2 LIF 70
--
-- Se APAGAN (fuera), no se borran: la cohorte es inmutable. Idempotente.
-- Las de parada del 20 al 23/08 (Rib Normal, Inter BLA, Microfibra PLO) son de
-- ANTES de la largada y quedan como están.
UPDATE scintela.parado_cohorte
   SET fuera = TRUE
 WHERE motivo = 'parado'
   AND NOT fuera
   AND fecha_marcado > (SELECT valor::date FROM scintela.parado_config
                         WHERE clave = 'largada');

-- El refresco rehace parado_venta y parado_foto desde la cohorte encendida.
UPDATE scintela.parado_refresh
   SET actualizado = NULL,
       detalle = 'esperando el refresco: después de la largada sólo entra segunda'
 WHERE id = 1;
