-- 0172 — QUE `fecha_modifica` SE SELLE SOLA.
--
-- Descubierto el 06/08/2026 investigando por qué no se podía reconstruir a
-- mano una cobranza: el cheque de PGQ del 06/08 canceló TRES facturas
-- (172916 · 2.394,37 · menos las notas 10410 y 11423 = 2.366,37 exacto) y las
-- tres quedaron con `usuario_modifica = 'alex'` y `fecha_modifica` en NULL.
--
-- O sea: no hay forma de preguntarle a la base "¿qué facturas se cobraron
-- entre las 17:17 y las 17:23?". La columna existe desde siempre y casi nadie
-- la escribe — de ~30 UPDATE sobre `scintela.factura` repartidos por seis
-- módulos, uno solo la setea (el de anulación).
--
-- 🚨 Por qué un TRIGGER y no arreglar los call sites: son decenas, están en
-- seis módulos distintos, y el que se escriba mañana se va a olvidar igual.
-- Una fecha de modificación no es una decisión del código de negocio — es una
-- propiedad de la fila. Va donde vive la fila.
--
-- Se aplica a TODA tabla de `scintela` que tenga la columna, presente y
-- futura: la lista sale de information_schema, no escrita a mano, que es
-- justamente el error que se está arreglando.
--
-- Ojo con lo que NO arregla: las filas viejas siguen en NULL. No se puede
-- inventar cuándo se modificaron. Desde acá para adelante, sí.

CREATE OR REPLACE FUNCTION scintela.sellar_fecha_modifica()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.fecha_modifica := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION scintela.sellar_fecha_modifica() IS
    'Mig 0172: sella fecha_modifica en cada UPDATE. Ver el comentario de la migración.';

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT c.table_name
          FROM information_schema.columns c
          JOIN information_schema.tables x
            ON x.table_schema = c.table_schema
           AND x.table_name  = c.table_name
           AND x.table_type  = 'BASE TABLE'
         WHERE c.table_schema = 'scintela'
           AND c.column_name  = 'fecha_modifica'
         ORDER BY c.table_name
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS tg_sellar_fecha_modifica ON scintela.%I', t);
        -- El nombre arranca con "tg_s" a propósito: los BEFORE de una misma
        -- tabla corren en orden alfabético y este tiene que ser el último, para
        -- que si otro trigger toca la fila la marca quede igual.
        EXECUTE format(
            'CREATE TRIGGER tg_sellar_fecha_modifica BEFORE UPDATE ON scintela.%I '
            'FOR EACH ROW EXECUTE FUNCTION scintela.sellar_fecha_modifica()', t);
    END LOOP;
END;
$$;
