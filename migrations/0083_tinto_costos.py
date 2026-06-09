"""Catálogo de costos de tintura — réplica PC del COSTOS.DBF del dBase.

Pedido dueña 2026-06-09: planilla para cargar tintura en PC ("lo mismo
que ingresan en el dbase, tienen que ingresar aca"). En el dBase
(MODIFICA.PRG → PROCEDURE TINTO) el operador ingresa COD + KG + KGN y
el programa busca el COD en F:\\STAND\\COSTOS.DBF para traer el nombre
del color y el costo $/kg → IMPORTE = KG × COSTO automático.

Ese COSTOS.DBF vive solo en la máquina de la fábrica, así que acá:
    1. Creamos scintela.tinto_costos (cod PK, color, costo).
    2. Seed desde el histórico de scintela.tinto: por cod con kg>0 e
       importe>0, costo = SUM(importe)/SUM(kg) (promedio ponderado) y
       color = el más reciente no vacío.
El catálogo es editable desde /informes/tinto-carga.

Idempotente: CREATE IF NOT EXISTS + ON CONFLICT DO NOTHING (no pisa
costos ya editados a mano si la migración se re-corre).
"""
from __future__ import annotations


def run(conn) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.tinto_costos (
            cod            varchar(5) PRIMARY KEY,
            color          varchar(30) NOT NULL DEFAULT '',
            costo          numeric(12,4) NOT NULL DEFAULT 0,
            fecha_crea     timestamp DEFAULT CURRENT_TIMESTAMP,
            fecha_modifica timestamp,
            usuario_crea   varchar(50),
            usuario_modifica varchar(50)
        )
        """
    )

    # Seed desde el histórico de tinto (la tabla es el mes en curso, pero
    # alcanza para los códigos de uso frecuente; el resto se cargan a mano).
    cur.execute(
        """
        INSERT INTO scintela.tinto_costos (cod, color, costo, usuario_crea)
        SELECT t.cod,
               COALESCE(MAX(NULLIF(TRIM(t.color), '')), '') AS color,
               ROUND(SUM(t.importe) / NULLIF(SUM(t.kg), 0), 4) AS costo,
               'migracion-0083'
          FROM scintela.tinto t
         WHERE COALESCE(NULLIF(TRIM(t.cod), ''), '') <> ''
           AND t.cod <> 'MAN'
           AND COALESCE(t.stat, '') NOT IN ('X', 'Y')
           AND COALESCE(t.kg, 0) > 0
           AND COALESCE(t.importe, 0) > 0
         GROUP BY t.cod
        HAVING SUM(t.kg) > 0
        ON CONFLICT (cod) DO NOTHING
        """
    )
    print(f"  seed tinto_costos: {cur.rowcount} códigos desde histórico tinto")
