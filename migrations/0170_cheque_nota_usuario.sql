-- 0170 — scintela.cheque.nota_usuario
--
-- TMT 2026-08-06. Alex pidió poder dejar una OBSERVACIÓN de texto libre a un
-- cheque de cliente ("por qué está postergado", "el cliente lo confirmó por
-- WhatsApp", etc.) y que se vea:
--   · en /cheques con un `*` rojo al lado del importe,
--   · impresa en la hoja de cheques ingresados (ingresados_dia.html) y en
--     la de resumen de cobranza del día (resumen_dia.html).
--
-- No reusamos `scintela.cheque.observacion` a propósito: esa columna es una
-- bitácora append-only con tags (`[E]` nota de edición, `[X]` error de carga,
-- `[REBOTE]`, `[C]` concepto histórico, `[ENDOSO a ...]`), escrita por ~15
-- callsites del backend. Un free-text mezclado ahí:
--   · dispararía el `*` rojo para CUALQUIER cheque que alguna vez se editó
--     o rebotó (falso positivo permanente en la lista),
--   · aparecería con tags encima en las hojas impresas,
--   · lo pisaría la próxima traza automática (RIGHT(...,200) trunca por izq).
-- Ver comentario del mismo estilo en 0158_cheque_concepto.sql.
--
-- Es TEXT NULL (no VARCHAR(N)): Alex prefiere no vivir con un cap arbitrario;
-- la validación de largo, si hace falta, vive en el form (mismo criterio que
-- otras notas del sistema — bancos.apertura.nota, importaciones.nota).
--
-- ⚠ El deploy NO corre migraciones automáticamente: la columna también se
-- bootstrapea en caliente desde `modules/cheques/nota_usuario.py::bootstrap_columna`
-- (mismo patrón que 0158). Este archivo existe para que el esquema quede
-- documentado y para entornos nuevos (fixtures, vista_local).

ALTER TABLE scintela.cheque
    ADD COLUMN IF NOT EXISTS nota_usuario TEXT;

COMMENT ON COLUMN scintela.cheque.nota_usuario IS
    'Observación de texto libre EDITABLE por el usuario (Alex). Se muestra '
    'como asterisco rojo en /cheques y se imprime en /cheques/ingresados-dia '
    'y /cheques/resumen-dia. NO confundir con `observacion` (bitácora '
    'append-only con tags del sistema).';
