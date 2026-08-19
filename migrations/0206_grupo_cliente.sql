-- Grupos de clientes: la tabla que ya vivia en produccion, ahora versionada.
--
-- TMT 2026-08-19 (duena, con las fotos del cuaderno): *"Estos clientes son de
-- un mismo grupo. en clientes una columna mas que diga grupo y ponemos el
-- primero codigo que aparece en lista"*.
--
-- `scintela.grupo_cliente` EXISTE en produccion desde mayo (24 filas, 19
-- grupos) pero NUNCA tuvo migracion: se creo a mano. Por eso
-- `modules/cartera/views.py::grupos` esta envuelto en un try/except global
-- ("si la tabla no existe, la pantalla no explota") y por eso la base local de
-- `vista_local.py` y la fixture de tests no la tienen -- cualquier pantalla
-- nueva que la joinee moriria en el sandbox y andaria en produccion, que es el
-- peor reparto posible.
--
-- Todo con IF NOT EXISTS: en produccion esto es un no-op salvo por las dos
-- restricciones nuevas.
--
-- La forma del dato: SOLO los HIJOS tienen fila. El padre NO se apunta a si
-- mismo. Un cliente sin fila es "sin grupo". Asi lo leen ya
-- `cartera/queries.py::aging_por_grupo` e
-- `informes/queries.py::estado_cuenta_clientes_saldos`, los dos con
-- `COALESCE(g.codigo_padre, cliente.codigo_cli)` -- o sea: sin fila, el cliente
-- ES su propio grupo. Cambiar esto ahora rompe a las dos.

CREATE TABLE IF NOT EXISTS scintela.grupo_cliente (
    codigo_padre     varchar(10) NOT NULL,
    codigo_hijo      varchar(10) NOT NULL,
    fecha_crea       timestamp   DEFAULT now(),
    fecha_modifica   timestamp,
    usuario_crea     varchar(50),
    usuario_modifica varchar(50)
);

-- Un cliente pertenece a UN grupo y nada mas. Sin esto, dos cargas del mismo
-- Excel dejan al cliente en dos grupos y el estado de cuenta lo imprime dos
-- veces, con el mismo saldo sumado dos veces al total.
ALTER TABLE scintela.grupo_cliente
    DROP CONSTRAINT IF EXISTS grupo_cliente_hijo_unico;
ALTER TABLE scintela.grupo_cliente
    ADD CONSTRAINT grupo_cliente_hijo_unico UNIQUE (codigo_hijo);

-- Nadie es hijo de si mismo (un grupo de uno no es un grupo).
ALTER TABLE scintela.grupo_cliente
    DROP CONSTRAINT IF EXISTS grupo_cliente_no_autoreferencia;
ALTER TABLE scintela.grupo_cliente
    ADD CONSTRAINT grupo_cliente_no_autoreferencia CHECK (codigo_padre <> codigo_hijo);

CREATE INDEX IF NOT EXISTS ix_grupo_cliente_padre
    ON scintela.grupo_cliente (codigo_padre);
