-- Las tres tablas del PORTAL DEL CLIENTE.
--
-- TMT 2026-08-24, PLAN_PORTAL_CLIENTE_2026_08_24.md. El cliente entra a ver su
-- estado de cuenta en vez de que se lo mandemos.
--
-- Como entra, decidido por la duena: codigo de 3 letras + RUC la primera vez,
-- ahi elige su clave, y de ahi en mas codigo + clave. Si la olvida, 6 digitos
-- por mail. El control lo hace el VENDEDOR despues y sin frenar a nadie: le
-- aparece en Mi Cartera que su cliente entro, con un boton para cortarle el
-- acceso.
--
-- Tres tablas y no una, porque son tres cosas con vidas distintas: el acceso
-- dura para siempre, la bitacora crece sin parar y los codigos vencen a los
-- 15 minutos.
--
-- ⚠ El portal NO pisa el maestro de clientes. El mail que el cliente corrija
-- queda ACA (`portal_acceso.mail`), y pasarlo a la ficha lo hace el vendedor o
-- la oficina desde la pantalla de siempre. De paso, de esta columna sale solo
-- el numero de cuantos lo cambiaron, que es lo que la duena queria medir.
--
-- Todo con IF NOT EXISTS: correrla dos veces es un no-op.

-- ---------------------------------------------------------------------------
-- El acceso: una fila por cliente
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scintela.portal_acceso (
    id_portal_acceso    serial PRIMARY KEY,
    -- El usuario ES el codigo de 3 letras. Normalizado igual que JOINea todo
    -- el sistema (ver la mig 0155 y el skill de codigos duplicados): sin el
    -- UPPER(TRIM(...)) un espacio al final seria un usuario distinto.
    codigo_cli          text NOT NULL,
    -- La clave, cifrada. NULL = todavia no eligio ninguna, o sea que el
    -- proximo ingreso es el primero y va con RUC.
    clave_hash          text,
    -- El mail que el cliente confirmo o corrigio en el primer ingreso. NO es
    -- el de la ficha: es el del portal. Se compara con el que teniamos para
    -- saber cuantos lo cambiaron.
    mail                text,
    mail_cambiado       boolean NOT NULL DEFAULT false,
    -- El vendedor puede cortarle el acceso desde Mi Cartera. No se borra la
    -- fila: queda el rastro de que existio y de quien lo corto.
    activo              boolean NOT NULL DEFAULT true,
    cortado_por         text,
    cortado_en          timestamptz,
    -- Frenos por intentos fallidos.
    intentos_fallidos   integer NOT NULL DEFAULT 0,
    bloqueado_hasta     timestamptz,
    primer_ingreso_en   timestamptz,
    ultimo_ingreso_en   timestamptz,
    creado_en           timestamptz NOT NULL DEFAULT now()
);

-- Un acceso por cliente, y normalizado: es la misma razon por la que el codigo
-- de cliente tiene indice unico normalizado en la mig 0155.
CREATE UNIQUE INDEX IF NOT EXISTS portal_acceso_codigo_unico
    ON scintela.portal_acceso (UPPER(TRIM(codigo_cli)));

-- ---------------------------------------------------------------------------
-- La bitacora: quien entro, cuando, y desde donde
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scintela.portal_ingreso (
    id_portal_ingreso   bigserial PRIMARY KEY,
    codigo_cli          text NOT NULL,
    -- 'ok' | 'clave_mala' | 'ruc_malo' | 'bloqueado' | 'cortado'
    resultado           text NOT NULL,
    -- Con que entro: 'ruc' el primer ingreso, 'clave' despues, 'codigo' el
    -- que llego por el mail de recuperacion.
    con_que             text,
    ip                  text,
    navegador           text,
    creado_en           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS portal_ingreso_cliente_fecha
    ON scintela.portal_ingreso (UPPER(TRIM(codigo_cli)), creado_en DESC);

-- Para la pantalla de "quien entro hoy" sin recorrer la tabla entera.
CREATE INDEX IF NOT EXISTS portal_ingreso_fecha
    ON scintela.portal_ingreso (creado_en DESC);

-- ---------------------------------------------------------------------------
-- Los 6 digitos de recuperacion
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scintela.portal_codigo (
    id_portal_codigo    bigserial PRIMARY KEY,
    codigo_cli          text NOT NULL,
    -- El codigo va CIFRADO, igual que una clave: el que pueda leer esta tabla
    -- no tiene que poder entrar a la cuenta de nadie.
    codigo_hash         text NOT NULL,
    mandado_a           text NOT NULL,
    vence_en            timestamptz NOT NULL,
    usado_en            timestamptz,
    creado_en           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS portal_codigo_cliente_vivo
    ON scintela.portal_codigo (UPPER(TRIM(codigo_cli)), vence_en DESC)
 WHERE usado_en IS NULL;
