-- 0135 — AÑO de la importación de cada anticipo USD (TMT 2026-07-29, dueña).
--
-- POR QUÉ: el número de la importación ("AC 58") se REUSA cada campaña — en las
-- últimas 400 importaciones de Asinfo hay 96 códigos repetidos sobre 278. El
-- cruce anticipo↔importación desempataba por "fecha más cercana", y cuando la
-- importación del año correcto todavía no existe en Asinfo el anticipo se
-- pegaba a la del año anterior (el AC 58 del 21/04/26 apuntaba a IM-0000527 en
-- vez de IM-0000622, mientras sus hermanos 57 y 59, cargados el MISMO día, sí
-- caían bien).
--
-- La primera versión (SHA ac155ea) puso el año DENTRO del concepto (`58/26`).
-- La dueña lo bajó, con razón: el concepto es lo que la gente lee y busca, y en
-- el dBase el BAP filtra por substring (BANCOS.PRG L751-753
-- `NUMER $ CONCEPTO`), así que tipear "26" traería todos los del año. Y sobre
-- todo: **el match anticipo↔importación NO EXISTE en el dBase** — es un dato de
-- Programa Core, no tiene por qué viajar en un campo compartido.
--
-- POR QUÉ UNA TABLA APARTE Y NO UNA COLUMNA EN scintela.dolares: DOLARES.DBF no
-- tiene `delete_where` en el TABLE_MAP de scripts/import_dbf.py → el sync hace
-- TRUNCATE ... RESTART IDENTITY de la tabla entera y la recarga con ids nuevos.
-- Una columna ahí se perdería en cada sincronización. Esta tabla NO está en el
-- TABLE_MAP, así que sobrevive (mismo patrón que scintela.importacion_pago).
--
-- POR QUÉ LA FIRMA Y NO id_dolares: el id se regenera en cada sync. La firma
-- (fecha + cta + concepto normalizado + importe + orden) sí es estable porque
-- sale del propio DBF. `orden` desempata las filas idénticas — existen: AC tiene
-- dos "18 TRA" de $1.298,50 (19/06 aplicado y 09/07 vivo), cargados por dos
-- operadores distintos.

CREATE TABLE IF NOT EXISTS scintela.dolares_anio (
    id_dolares_anio  SERIAL PRIMARY KEY,
    -- firma estable (sobrevive al TRUNCATE del sync)
    fecha            DATE           NOT NULL,
    cta              VARCHAR(3)     NOT NULL,
    concepto_norm    VARCHAR(100)   NOT NULL,   -- UPPER(TRIM(concepto))
    importe          NUMERIC(14,2)  NOT NULL,
    orden            SMALLINT       NOT NULL DEFAULT 0,
    -- el dato
    anio             SMALLINT       NOT NULL,
    -- auditoría
    origen           VARCHAR(20)    NOT NULL DEFAULT 'manual',  -- manual | sugerencia
    usuario_modifica VARCHAR(50),
    fecha_modifica   TIMESTAMPTZ    DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dolares_anio_firma
    ON scintela.dolares_anio (fecha, cta, concepto_norm, importe, orden);

-- El lookup de la lista de /dolares es por (cta, fecha): índice de apoyo.
CREATE INDEX IF NOT EXISTS ix_dolares_anio_cta_fecha
    ON scintela.dolares_anio (cta, fecha);
