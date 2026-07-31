-- 0146 — Las NOVEDADES se leen POR PERSONA, no de a una para todos.
--
-- TMT 2026-07-31 (dueña): *"las notificaciones tienen que ser por persona. si
-- yo la leo igual le tienen que aparecer no leídas a federico andres y alex"*.
--
-- Hasta acá `aviso.leido` era UN booleano de la fila: el primero que abría la
-- campanita apagaba el contador de TODO el mundo. Y como abrir la campanita ES
-- leerla (30/07), alcanzaba con que Tamara entrara al programa a la mañana
-- para que Federico, Andrés y Alex no se enteraran nunca de que el programa
-- había cargado algo solo. El aviso seguía en /novedades, pero nadie lo miraba
-- porque nada le avisaba que estuviera.
--
-- Ahora "leído" es una relación aviso↔persona: una fila acá = esa persona ya
-- lo vio. Sin fila = no lo vio, aunque el de al lado sí.
--
-- La columna vieja `aviso.leido` NO se toca y sigue valiendo como "leído por
-- todos": es la línea de base para los avisos que ya estaban marcados antes de
-- esta migración. Sin eso, aplicarla haría reaparecer de golpe como NUEVO todo
-- el historial ya leído, que es exactamente el ruido que vacía una campanita.
-- De acá en adelante `marcar_leidos` sólo escribe en esta tabla.
--
-- ARCHIVADO (mig 0145) sigue siendo GLOBAL a propósito: "ya no aplica" es un
-- hecho del mundo, no una opinión de cada uno; "leído" sí es de cada uno.

CREATE TABLE IF NOT EXISTS scintela.aviso_leido (
    id_aviso  integer     NOT NULL
              REFERENCES scintela.aviso (id_aviso) ON DELETE CASCADE,
    usuario   varchar(60) NOT NULL,
    leido_en  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id_aviso, usuario)
);

-- El contador de la campanita pregunta "¿qué NO leyó esta persona?" en cada
-- carga de pantalla: se entra por usuario.
CREATE INDEX IF NOT EXISTS ix_aviso_leido_usuario
    ON scintela.aviso_leido (usuario, id_aviso);
