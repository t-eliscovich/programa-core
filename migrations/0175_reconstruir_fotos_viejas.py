"""Reconstruye lo que se pueda del detalle de las fotos anteriores a la grabadora.

TMT 2026-08-06: *"sin detalle — foto vieja, esto quiero intentar recomponer"*.

Las ~200 fotos anteriores a la mig 0171 guardan los once totales y nada más.
El detalle por documento no se guardó, pero una parte SÍ se puede recuperar:
desde que se retiró el dBase, `fecha_crea` es un timestamp real, así que se
puede preguntar qué documentos NACIERON en cada ventana de cinco minutos.

🚨 Lo que NO se puede, y hay que decirlo: lo APLICADO. Aplicar un cheque a una
factura no dejaba fecha (ese es el bug que arregla la mig 0172), así que una
cobranza de esta mañana no se puede fechar y no aparece. Por eso todo lo que
sale de acá queda marcado `tipo = 'reconstruido'`:

  · la pantalla lo dice — "reconstruido de los documentos, puede faltar";
  · el aviso de "ventanas que no cierran" las IGNORA. Si contaran, cada foto
    vieja aparecería como un problema, y no lo es: nunca tuvo registro.
    Serían 200 tareas inventadas tapando las de verdad.

Lo que sí se recupera, con el aporte firmado hacia la utilidad:

  facturas  +importe   cheques  +importe
  caja      +/-importe posdat   -importe   (un pasivo que sube la baja)

`fecha_crea` es `timestamp` sin zona guardando UTC; `creado_en` de la foto es
`timestamptz`. De ahí el `AT TIME ZONE 'UTC'`.

Idempotente: sólo toca fotos que hoy no tienen NINGÚN movimiento.
"""

from __future__ import annotations

import os

#: (tabla, id, componente, signo, expresión del importe, expresión de la etiqueta)
FUENTES = (
    ("factura", "id_factura", "facturas", 1, "COALESCE(d.importe, 0)",
     "'Factura ' || COALESCE(NULLIF(TRIM(d.numf_completo), ''), d.numf::text)"
     " || ' · ' || COALESCE(TRIM(d.codigo_cli), '?')", "Venta facturada"),
    ("cheque", "id_cheque", "cheques", 1, "COALESCE(d.importe, 0)",
     "'Cheque ' || COALESCE(TRIM(d.no_cheque), '?')"
     " || ' · ' || COALESCE(TRIM(d.codigo_cli), '?')", "Cheque recibido"),
    ("caja", "id_caja", "caja", 1,
     "CASE WHEN d.tipo = 'S' THEN -COALESCE(d.importe, 0) ELSE COALESCE(d.importe, 0) END",
     "'Caja ' || COALESCE(TRIM(d.tipo), '') || ' · ' || LEFT(COALESCE(TRIM(d.concepto), ''), 60)",
     "Movimiento de caja"),
    ("posdat", "id_posdat", "totp", -1, "COALESCE(d.importe, 0)",
     "'Deuda ' || COALESCE(TRIM(d.prov), '') || ' ' || COALESCE(d.num::text, '')",
     "Deuda nueva cargada"),
)

VENTANAS = """
WITH v AS (
    SELECT id_traza, creado_en,
           LAG(creado_en) OVER (ORDER BY creado_en, id_traza) AS desde
      FROM scintela.traza_utilidad
), sin_detalle AS (
    SELECT v.* FROM v
     WHERE v.desde IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM scintela.dia_movimiento m
                        WHERE m.id_traza = v.id_traza)
)
"""


def run(conn) -> None:
    cur = conn.cursor()
    total = 0
    for tabla, pk, comp, signo, importe, etiqueta, regla in FUENTES:
        cur.execute(
            VENTANAS + f"""
            INSERT INTO scintela.dia_movimiento
                (id_traza, componente, tipo, doc_id, etiqueta,
                 delta, aporte, regla, familia)
            SELECT s.id_traza, %s, 'reconstruido',
                   %s || d.{pk}::text, {etiqueta},
                   ROUND(({importe})::numeric, 2),
                   ROUND(({importe} * %s)::numeric, 2),
                   %s, %s
              FROM sin_detalle s
              JOIN scintela.{tabla} d
                ON d.fecha_crea AT TIME ZONE 'UTC' >  s.desde
               AND d.fecha_crea AT TIME ZONE 'UTC' <= s.creado_en
             WHERE COALESCE(d.importe, 0) <> 0
            """,
            (comp, comp[0], signo, regla,
             "traspaso" if comp == "cheques" else "utilidad"),
        )
        total += cur.rowcount
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    {total} movimiento(s) reconstruido(s) de fotos viejas")
