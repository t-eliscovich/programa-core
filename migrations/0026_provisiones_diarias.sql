-- Migration 0026: tabla scintela.sistema_meta y soporte para provisiones diarias.
--
-- Contexto: dBase MENU.PRG líneas 282-333 aplica "provisiones diarias"
-- automáticas cada vez que abre el sistema en un día nuevo (que no sea
-- domingo). Suma cantidades fijas a posdats específicos identificados
-- por PROV+CONCEPTO (sueldos, IVA, alquiler, imp. renta, etc.).
--
-- Total por día hábil: ~$31,000. Si nuestro PG no las corre, la deuda
-- (PASIVOS) se va atrasando vs dBase un día por día.
--
-- Esta migración:
--   1. Crea una tabla genérica scintela.sistema_meta clave/valor para
--      tracking de última corrida (entre otras cosas).
--   2. Inicializa la clave 'provisiones_diarias_ult_fecha' = ayer, para
--      que el primer call de hoy aplique exactamente 1 día (idempotente).

CREATE TABLE IF NOT EXISTS scintela.sistema_meta (
    clave       VARCHAR(64) PRIMARY KEY,
    valor       TEXT NOT NULL,
    actualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE scintela.sistema_meta IS
    'Clave/valor para flags y markers del sistema. P.ej. fechas de '
    'última ejecución de jobs idempotentes (provisiones diarias).';

-- Inicializa la fecha de provisiones diarias. Si la clave ya existe
-- (re-run de migración), no la pisa.
INSERT INTO scintela.sistema_meta (clave, valor)
VALUES ('provisiones_diarias_ult_fecha', (CURRENT_DATE - INTERVAL '1 day')::date::text)
ON CONFLICT (clave) DO NOTHING;
