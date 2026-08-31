-- 0237 · Código del programa cargable para una importación cuya Nota no lo trae
--
-- Dueña 31/08/2026, sobre MTG3756 ("MTG3756 ---1 / ---2", sin "( MD 1 )"):
-- *"¿no podemos hacer que valga como está cargado?"*. Todos los cruces
-- (anticipos, kg, costo, vigías) agarran el código DE LA NOTA de Asinfo; si
-- la Nota viene pelada, hasta hoy la única salida era editarla allá.
--
-- Esta tabla guarda el código cargado A MANO desde /importaciones (columna
-- Código, filas sin código). El programa se lo APPENDEA a la nota al leerla
-- de Asinfo, así el resto del código no cambia: parsea lo mismo que si el
-- proveedor lo hubiera escrito.
CREATE TABLE IF NOT EXISTS scintela.importacion_codigo (
    im_numero    text PRIMARY KEY,
    codigo       text NOT NULL,
    usuario_crea text,
    fecha_crea   timestamptz NOT NULL DEFAULT now()
);
