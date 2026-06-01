-- Test-only sanitized legacy baseline for Programa Core DB integration tests.
--
-- This is not production data. It models the pre-migration dBase/Postgres
-- tables that current migrations expect to find before applying the overlay
-- chain in migrations/.

BEGIN;

DROP SCHEMA IF EXISTS scintela CASCADE;
DROP SCHEMA IF EXISTS seguridad CASCADE;

CREATE SCHEMA scintela;
CREATE SCHEMA seguridad;

CREATE TABLE seguridad.rol (
    id_rol      SERIAL PRIMARY KEY,
    nombre_rol VARCHAR(50) NOT NULL UNIQUE,
    descripcion TEXT
);

CREATE TABLE seguridad.usuario (
    id_usuario      SERIAL PRIMARY KEY,
    username        VARCHAR(50) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    nombre_completo VARCHAR(100),
    id_rol          INTEGER,
    activo          BOOLEAN DEFAULT TRUE,
    fecha_crea      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica  TIMESTAMP
);

CREATE TABLE seguridad.permiso (
    id_permiso    SERIAL PRIMARY KEY,
    id_rol        INTEGER,
    nombre_opcion VARCHAR(100) NOT NULL,
    UNIQUE (id_rol, nombre_opcion)
);

CREATE TABLE scintela.banco (
    no_banco INTEGER PRIMARY KEY,
    nombre VARCHAR(50),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.factura (
    id_factura SERIAL PRIMARY KEY,
    numf INTEGER NOT NULL,
    fecha DATE NOT NULL,
    codigo_cli VARCHAR(5) NOT NULL,
    kg NUMERIC(9,2) NOT NULL DEFAULT 0,
    importe NUMERIC(9,2) NOT NULL DEFAULT 0,
    abono NUMERIC(9,2) DEFAULT 0,
    saldo NUMERIC(9,2) DEFAULT 0,
    stat VARCHAR(2),
    condic VARCHAR(2),
    vencimiento DATE,
    tipo VARCHAR(2),
    clave VARCHAR(2),
    pase VARCHAR(5),
    id_documento INTEGER,
    numf_completo VARCHAR(20),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.cheque (
    id_cheque SERIAL PRIMARY KEY,
    no_cheque VARCHAR(10),
    fecha DATE NOT NULL,
    fechad DATE NOT NULL,
    codigo_cli VARCHAR(5),
    importe NUMERIC(9,2),
    no_banco INTEGER,
    banco VARCHAR(30),
    stat VARCHAR(5),
    fechaing DATE,
    fechaout DATE,
    prov VARCHAR(5),
    clave VARCHAR(5),
    numero_transaccion INTEGER,
    id_cheque_padre INTEGER,
    pasaconta INTEGER,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.compra (
    id_compra SERIAL PRIMARY KEY,
    fecha DATE,
    id_proveedor INTEGER,
    codigo_prov VARCHAR(3),
    tipo VARCHAR(3),
    comprobante VARCHAR(100),
    kg NUMERIC(9,2),
    importe NUMERIC(9,2),
    numero INTEGER,
    fecha_ing DATE,
    fechad DATE,
    concepto VARCHAR(200),
    clave VARCHAR(3),
    no_banco INTEGER,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.posdat (
    id_posdat SERIAL PRIMARY KEY,
    fecha DATE,
    fechad DATE,
    prov VARCHAR(3),
    num INTEGER,
    importe NUMERIC(9,2),
    concepto VARCHAR(100),
    banc INTEGER,
    clave VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.transacciones_bancarias (
    id_transaccion SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    documento VARCHAR(5) NOT NULL,
    concepto VARCHAR(50) NOT NULL,
    fechad DATE,
    importe NUMERIC(9,2) NOT NULL DEFAULT 0,
    saldo NUMERIC(9,2) DEFAULT 0,
    stat VARCHAR(2),
    no_banco INTEGER,
    no_cta VARCHAR(20),
    prov VARCHAR(5),
    numreferencia INTEGER,
    clave VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.caja (
    id_caja SERIAL PRIMARY KEY,
    fecha DATE,
    tipo VARCHAR(3),
    importe NUMERIC(9,2),
    concepto VARCHAR(100),
    saldo NUMERIC(9,2),
    clave VARCHAR(3),
    id_cheque INTEGER,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.capital (
    id_capital SERIAL PRIMARY KEY,
    fecha DATE,
    doc VARCHAR(5),
    concepto VARCHAR(50),
    importe NUMERIC(21,2),
    invanual NUMERIC(21,2),
    capital NUMERIC(21,2),
    util NUMERIC(21,2),
    patri NUMERIC(21,2),
    clave VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.retencion (
    id_retencion SERIAL PRIMARY KEY,
    codigo_cli VARCHAR(50),
    rete NUMERIC(9,2),
    numf INTEGER,
    fecha DATE DEFAULT CURRENT_TIMESTAMP,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.flujo (
    id_flujo SERIAL PRIMARY KEY,
    fecha DATE,
    cheques NUMERIC(9,2),
    facturas NUMERIC(9,2),
    posdat2 NUMERIC(9,2),
    inter NUMERIC(9,2),
    posdat1 NUMERIC(9,2),
    pichincha NUMERIC(9,2),
    mprima NUMERIC(9,2),
    gastos NUMERIC(9,2),
    saldo NUMERIC(9,2),
    pagos NUMERIC(9,2),
    dolares NUMERIC(9,2),
    usaldo NUMERIC(9,2),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.cliente (
    id_cliente SERIAL PRIMARY KEY,
    codigo_cli VARCHAR(5) NOT NULL UNIQUE,
    nombre VARCHAR(200),
    telefono VARCHAR(30),
    ruc VARCHAR(16),
    correo VARCHAR(50),
    direccion1 VARCHAR(200),
    direccion2 VARCHAR(200),
    stop VARCHAR(1),
    cupo INTEGER,
    fecha_cupo DATE,
    clave VARCHAR(3),
    pase VARCHAR(2),
    id_ubicacion INTEGER,
    no_banco INTEGER,
    descuento NUMERIC(9,2),
    pago VARCHAR(2),
    observacion VARCHAR(200),
    vend VARCHAR(50),
    provincia VARCHAR(50),
    canton VARCHAR(50),
    parroquia VARCHAR(50),
    cliente VARCHAR(50),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.proveedor (
    id_proveedor SERIAL PRIMARY KEY,
    codigo_prov VARCHAR(3),
    nombre VARCHAR(200),
    telefono VARCHAR(30),
    ruc VARCHAR(16),
    correo VARCHAR(50),
    direccion VARCHAR(200),
    representante VARCHAR(200),
    tipo VARCHAR(1),
    plazo INTEGER,
    retbase INTEGER,
    retiva INTEGER,
    codigo_imp INTEGER,
    activo VARCHAR(2),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.chequesxfact (
    id_chequexfact SERIAL PRIMARY KEY,
    id_cheque INTEGER,
    id_fact INTEGER,
    fechaing DATE NOT NULL DEFAULT CURRENT_DATE,
    codigo_cli VARCHAR(5),
    importe NUMERIC(9,2),
    no_banco INTEGER,
    tipo VARCHAR(5),
    stat_f VARCHAR(5),
    fecha_venci_f DATE,
    abono_f NUMERIC(9,2),
    saldo_f NUMERIC(9,2),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.chequextransaccion (
    id_chequextransacc SERIAL PRIMARY KEY,
    id_cheque INTEGER,
    id_transaccion INTEGER,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    stat_ch VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.dolares (
    id_dolares SERIAL PRIMARY KEY,
    fecha DATE,
    cta VARCHAR(3),
    concepto VARCHAR(100),
    importe NUMERIC(9,2),
    st VARCHAR(3),
    clave VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.retiros (
    id_retiro SERIAL PRIMARY KEY,
    fecha DATE,
    nb INTEGER,
    ret NUMERIC(9,2),
    de VARCHAR(5),
    concepto VARCHAR(100),
    clave VARCHAR(5),
    id_transaccion_bancaria INTEGER,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.xgast (
    id_xgast SERIAL PRIMARY KEY,
    fecha DATE,
    doc VARCHAR(5),
    prov VARCHAR(5),
    concepto VARCHAR(100),
    num INTEGER,
    fechad DATE,
    importe NUMERIC(9,2),
    saldo NUMERIC(9,2),
    stat VARCHAR(3),
    clave VARCHAR(3),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.activos (
    id_activos SERIAL PRIMARY KEY,
    fecha DATE,
    concepto VARCHAR(100),
    tipo VARCHAR(3),
    inicial NUMERIC(9,2),
    amortizac NUMERIC(9,2),
    amortimes NUMERIC(9,2),
    valor NUMERIC(9,2),
    cuota NUMERIC(9,2),
    vida_util INTEGER,
    id_proveedor INTEGER,
    ult_mes_amortizado INTEGER,
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_modifica TIMESTAMP,
    usuario_crea VARCHAR(50),
    usuario_modifica VARCHAR(50)
);

CREATE TABLE scintela.provisiones (
    id_provisiones SERIAL PRIMARY KEY,
    concepto VARCHAR(50),
    importe NUMERIC(9,2),
    periodo_aplica VARCHAR(30),
    fecha_crea TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_actualiza TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    usuario_crea VARCHAR(20),
    usuario_actualiza VARCHAR(30)
);

COMMIT;
