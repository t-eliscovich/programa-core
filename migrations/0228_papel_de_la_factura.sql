-- Caché de la factura EN PAPEL, la copia que el vendedor le manda al cliente
--
-- TMT 2026-08-26 (dueña): *"que la factura imite exactamente como lo hace
-- asinfo, así no piensan que es distinta"*.
--
-- La hoja necesita más que el detalle: el recuadro del SRI (que vive en
-- `factura_clienteSRI`), los datos del cliente y los renglones en el orden en
-- que Asinfo los imprime. Todo eso es UNA consulta a Asinfo, y cruzar el
-- puente de Metabase cuesta 650 ms fijos aunque la consulta sea tonta.
--
-- Una factura emitida no cambia nunca, así que se pregunta una sola vez. Es la
-- misma decisión —y la misma forma— que `factura_detalle` (mig 0218), en una
-- tabla aparte porque guarda otra respuesta a otra pregunta: mezclarlas
-- obligaría a invalidar las dos cuando cambia una.
--
-- ⚠ Es una CACHÉ: se puede vaciar entera sin perder nada.
CREATE TABLE IF NOT EXISTS scintela.factura_papel (
    numero      varchar(24) PRIMARY KEY,
    datos       jsonb       NOT NULL,
    fecha_crea  timestamptz NOT NULL DEFAULT now()
);
