"""La hora a la que Asinfo emitió cada documento.

TMT 2026-09-02 (dueña, mirando /facturas/183296): *"podés agregarles hora y
minutos de emitida. no segundos"*.

`scintela.factura.fecha` es un DATE —el dBase nunca supo la hora— y Asinfo
la tiene en `factura_cliente.fecha_creacion`, que está en hora de ECUADOR
(el reloj del servidor está en UTC, esa columna no; ver
`despacho_sin_factura.py`). Se pregunta una vez y se guarda en
`factura.hora_emision` (mig 0239): la ficha no cruza el puente dos veces por
la misma factura.

Fail-soft como todo el puente: si Metabase no contesta, no hay hora y la
ficha se abre igual. Un "no pude preguntar" NO se guarda — la próxima
apertura vuelve a preguntar.
"""
from __future__ import annotations

import logging
import re

_LOG = logging.getLogger("programa_core.asinfo.hora_emision")

#: Sólo números con la forma que Asinfo conoce. Es la misma regla que en
#: `factura_lineas`, y además la defensa del `IN (...)`: la SQL se arma con
#: los números adentro, así que nada que no sea dígitos y guiones pasa.
_NUMERO_RE = re.compile(r"^(?:\d{3}-\d{3}-\d{9}|(?:NTEN|NCNT)-\d{1,9})$")

DB_ASINFO = 2


def _sql(numeros: list[str]) -> str:
    lista = ", ".join(f"'{n}'" for n in numeros)
    # 108 = 'hh:mi:ss'; con varchar(5) quedan sólo 'hh:mi' — sin segundos,
    # como pidió la dueña.
    return f"""
SELECT LTRIM(RTRIM(fc.numero))                        AS numero,
       CONVERT(varchar(5), fc.fecha_creacion, 108)    AS hora
  FROM factura_cliente fc
 WHERE fc.numero IN ({lista})
   AND fc.estado <> 0
"""


def horas(numeros) -> dict[str, str]:
    """{numero: 'HH:MM'} para los números que Asinfo conoce.

    Devuelve {} si no hay puente, si Metabase no contestó o si ningún número
    tiene la forma de Asinfo. Una sola consulta para todos: cruzar el puente
    cuesta lo mismo por uno que por cien.
    """
    limpios = sorted({str(n or "").strip() for n in numeros})
    limpios = [n for n in limpios if _NUMERO_RE.match(n)]
    if not limpios:
        return {}
    from modules._lib import metabase_client

    if not metabase_client.disponible():
        return {}
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql(limpios), max_results=max(1000, len(limpios)))
    except Exception as e:  # noqa: BLE001 — fail-soft, como todo el puente
        _LOG.warning("hora de emisión: %s", e)
        return {}
    if not ok:
        return {}
    out: dict[str, str] = {}
    for f in filas or []:
        n = str(f.get("numero") or "").strip()
        h = str(f.get("hora") or "").strip()[:5]
        if n and len(h) == 5 and h[2] == ":":
            out[n] = h
    return out


def completar(facturas) -> int:
    """Guarda la hora de las facturas que no la tienen. Devuelve cuántas.

    `facturas`: filas con `id_factura` y `numf_completo` (dicts). Las que ya
    tienen `hora_emision` se saltan sin preguntar.
    """
    pendientes = [f for f in facturas
                  if f.get("numf_completo") and not f.get("hora_emision")]
    if not pendientes:
        return 0
    por_numero = horas(f["numf_completo"] for f in pendientes)
    if not por_numero:
        return 0
    from modules.facturas import queries

    n = 0
    for f in pendientes:
        h = por_numero.get(str(f["numf_completo"]).strip())
        if not h:
            continue
        try:
            queries.guardar_hora_emision(int(f["id_factura"]), h)
        except Exception as e:  # noqa: BLE001 — no guardar no rompe nada
            _LOG.warning("guardando la hora de %s: %s", f.get("numf_completo"), e)
            continue
        f["hora_emision"] = h
        n += 1
    return n
