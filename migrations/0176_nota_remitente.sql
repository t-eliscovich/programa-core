-- 0176 — De qué dirección sale la nota.
--
-- SES exige que el remitente esté verificado, y el que se verificó el 06/08
-- es teliscovich@gmail.com (la cuenta está en sandbox, así que remitente y
-- destinatario tienen que estar los dos verificados — siendo el mismo, con
-- una verificación alcanza).
--
-- 🚨 Va en la BASE y no en una variable de entorno del EC2 por la misma razón
-- que los destinatarios: cambiar de dirección no puede requerir entrar al
-- server. El entorno sigue mandando si está seteado, para poder pisarlo en
-- una emergencia sin tocar datos.

CREATE TABLE IF NOT EXISTS scintela.nota_config (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

INSERT INTO scintela.nota_config (clave, valor)
VALUES ('remitente', 'teliscovich@gmail.com')
ON CONFLICT (clave) DO NOTHING;
