-- Migration 0032: scintela.vendedor + backfill desde cliente.vend.
--
-- Pedido dueña 2026-05-18 (docx "Para Claude"): módulo de Comisiones de
-- vendedores. Replica MODIFICA.PRG PROCEDURE COMISION (línea 1770) que
-- listaba cobranzas del mes filtradas por cliente.vend = código.
--
-- dBase NO guardaba el % de comisión por vendedor (la dueña lo calculaba
-- a mano). Acá lo agregamos para poder mostrar el monto de comisión
-- calculado automáticamente.
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS scintela.vendedor (
    codigo            VARCHAR(3)   PRIMARY KEY,
    nombre            VARCHAR(100),
    pct_comision      NUMERIC(5,2) DEFAULT 0,
    activo            BOOLEAN      DEFAULT TRUE,
    fecha_crea        DATE         DEFAULT CURRENT_DATE,
    fecha_actualiza   TIMESTAMP,
    usuario_actualiza VARCHAR(30)
);

COMMENT ON TABLE scintela.vendedor IS
    'Vendedores con % de comisión. Cruza con scintela.cliente.vend.';
COMMENT ON COLUMN scintela.vendedor.pct_comision IS
    'Porcentaje de comisión sobre cobranzas efectivamente acreditadas en banco.';

-- Backfill: insertar un registro por cada `vend` distinto que ya existe
-- en cliente.vend (excluyendo blanco/null). Idempotente (ON CONFLICT).
INSERT INTO scintela.vendedor (codigo, nombre)
SELECT DISTINCT UPPER(TRIM(vend)) AS codigo,
       UPPER(TRIM(vend))         AS nombre  -- placeholder; la dueña edita después
FROM scintela.cliente
WHERE vend IS NOT NULL
  AND TRIM(vend) <> ''
ON CONFLICT (codigo) DO NOTHING;

-- TMT 2026-05-18 — si el runner corre como postgres pero la app usa otro
-- user, hay que pasarle ownership. El SKILL.md (migrate.py) documenta
-- exactamente este caso: "must be owner" → ALTER OWNER. Acá lo hacemos
-- automático para el user actual de la sesión (current_user devuelve
-- quien corre el script — si fue postgres, el ALTER es no-op; si fue
-- otro user, se le pasa al app user). Idempotente.
DO $$
DECLARE
    app_user TEXT := current_user;
BEGIN
    IF app_user <> 'postgres' THEN
        EXECUTE format('ALTER TABLE scintela.vendedor OWNER TO %I', app_user);
    END IF;
END $$;
