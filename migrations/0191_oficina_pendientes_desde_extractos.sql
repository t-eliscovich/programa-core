-- 0191_oficina_pendientes_desde_extractos.sql
-- Completar de una vez la OFICINA de los pendientes de banco. 2026-08-13.
--
-- Dueña: *"necesito que me pongas oficina"* y, sobre el botón que le ofrecí
-- para hacerlo a pedido: *"no quiero botón, siempre deberían venir puestas;
-- ahora hacelo a la fuerza para no perder el dato"*. Así que va acá, una vez,
-- y de acá en adelante lo hace solo cada vez que se sube un extracto
-- (`sesion.completar_oficinas_pendientes`, llamado desde `banco_crear_sesion`).
--
-- Por qué faltaban: los pendientes salieron de la hoja FEB2023, que no tiene
-- columna de oficina. Medido el 13/08 en producción: 0 de 189 con oficina.
-- El MISMO movimiento sí la trae en los extractos del Pichincha ya subidos,
-- guardados en `banco_conciliacion_sesion.extracto_payload` (2.855 filas,
-- todas con oficina). Cruzando por fecha + documento + monto + tipo —la misma
-- firma con la que `estado_sesion` ya decide que un renglón del extracto y un
-- pendiente son el mismo movimiento— se completan 159 de 189. Las 30 que
-- quedan son de abril/mayo, anteriores al extracto más viejo que tenemos:
-- se completan solas el día que se suba el extracto de esos meses.
--
-- Sólo toca las vacías y sólo cuando el cruce es inequívoco: si dos filas del
-- extracto comparten la firma pero dicen oficinas distintas, se saltea en vez
-- de elegir cualquiera. Mejor vacía que inventada.

WITH ex AS (
    SELECT (m->>'fecha')::date                       AS fecha,
           TRIM(m->>'documento')                     AS documento,
           ROUND((m->>'monto')::numeric, 2)          AS monto,
           UPPER(LEFT(COALESCE(m->>'tipo', 'C'), 1)) AS tipo,
           MIN(TRIM(m->>'oficina'))                  AS oficina
      FROM scintela.banco_conciliacion_sesion s,
           jsonb_array_elements(s.extracto_payload) AS m
     WHERE jsonb_typeof(s.extracto_payload) = 'array'
       AND NULLIF(TRIM(COALESCE(m->>'oficina', '')), '') IS NOT NULL
       AND NULLIF(TRIM(COALESCE(m->>'documento', '')), '') IS NOT NULL
     GROUP BY 1, 2, 3, 4
    HAVING COUNT(DISTINCT TRIM(m->>'oficina')) = 1
)
UPDATE scintela.banco_historicos_pendientes h
   SET oficina = ex.oficina
  FROM ex
 WHERE NULLIF(TRIM(COALESCE(h.oficina, '')), '') IS NULL
   AND h.fecha = ex.fecha
   AND TRIM(h.documento) = ex.documento
   AND ROUND(h.monto, 2) = ex.monto
   AND UPPER(LEFT(COALESCE(h.tipo, 'C'), 1)) = ex.tipo;
