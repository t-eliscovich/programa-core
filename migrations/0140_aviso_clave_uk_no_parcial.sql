-- 0140 — el índice único de `aviso.clave` deja de ser PARCIAL.
--
-- BUG de la 0139, encontrado antes de que se vea en vivo: el índice se creó como
-- `UNIQUE (clave) WHERE clave IS NOT NULL`, y Postgres NO acepta un índice
-- parcial como árbitro de un `ON CONFLICT (clave)` sin repetir el predicado —
-- tira 42P10 "there is no unique or exclusion constraint matching the ON
-- CONFLICT specification". Y como `avisar()` es fail-soft (un aviso roto no
-- puede tumbar la carga que avisaba), el error se habría tragado en silencio:
-- las cargas seguían andando y la campanita nunca decía nada.
--
-- Un índice único NO parcial hace lo mismo para este caso: en Postgres los NULL
-- son distintos entre sí, así que los avisos sin clave (los que no necesitan
-- anti-repetido) siguen entrando todos.

DROP INDEX IF EXISTS scintela.aviso_clave_uk;

CREATE UNIQUE INDEX IF NOT EXISTS aviso_clave_uk ON scintela.aviso (clave);
