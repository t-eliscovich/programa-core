"""Salidas de material que Asinfo registró pero que NO bajaron el saldo.

Tamara 2026-10-01. Desde el 13/07/2026 (primer día hábil después de una
actualización de Asinfo del 10/07), a veces, escaneando lotes en una salida de
material (SM), el lote queda en la salida pero el saldo de la bodega no baja:
el lote sigue figurando en stock aunque ya se fue a producción. En el hilo
pasó en 58 de 1.034 salidas (jul–sep) y dejó ~52.000 kg de hilo "de más" en el
balance — ~124 mil de utilidad inflada sólo en septiembre. Nadie lo vio porque
el saldo equivocado es el del propio Asinfo.

Cómo se detecta: por cada lote de una SM de los últimos días, el último saldo
del lote en `saldo_producto_lote` sigue positivo y es de una fecha ANTERIOR a
la salida (cuando la salida anda bien, Asinfo graba un saldo nuevo en el mismo
instante). También marca los lotes de HILO cargados en dos o más salidas (las
"etiquetas repetidas en dos órdenes" que reportó Alex el 30/09). En tela cruda
NO: ahí un rollo se reparte entre dos órdenes seguidas todo el tiempo y el
saldo baja bien (medido el 01/10: decenas de casos por día, todos normales).

Sólo lee Asinfo (vía Metabase). Fail-soft: si Asinfo no contesta, no alarma.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("programa_core.asinfo.salidas_sin_saldo")

BODEGAS = {51: "Hilo", 52: "Tela cruda"}


def _sql(dias: int) -> str:
    dias = max(1, min(int(dias), 60))
    return f"""
WITH sal AS (
    SELECT ip.numero_documento doc, ip.id_bodega b, ip.id_producto, ip.id_lote,
           ip.cantidad, ip.fecha_creacion, ip.id_detalle_movimiento_inventario idm
      FROM inventario_producto ip
     WHERE ip.id_bodega IN (51, 52) AND ip.operacion = -1
       AND ISNULL(ip.indicador_anulacion, 0) = 0
       AND ip.numero_documento LIKE 'SM-%'
       AND ip.fecha >= DATEADD(day, -{dias}, CAST(GETDATE() AS date))
       AND ip.fecha_creacion < DATEADD(minute, -15, GETDATE())
),
snap AS (
    SELECT s.id_producto, s.id_bodega, s.id_lote, s.saldo, s.sf FROM (
        SELECT x.id_producto, x.id_bodega, x.id_lote, x.saldo, x.fecha sf,
               ROW_NUMBER() OVER (PARTITION BY x.id_producto, x.id_bodega, x.id_lote
                                  ORDER BY x.fecha DESC, x.id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote x
          JOIN (SELECT DISTINCT id_producto, b, id_lote FROM sal) k
            ON k.id_producto = x.id_producto AND k.b = x.id_bodega AND k.id_lote = x.id_lote
    ) s WHERE rn = 1
),
rep AS (SELECT b, id_lote FROM sal WHERE b = 51
         GROUP BY b, id_lote HAVING COUNT(DISTINCT doc) > 1),
x AS (
    SELECT sal.*,
           CASE WHEN s.saldo > 0.5 AND s.sf < CAST(sal.fecha_creacion AS date)
                THEN 1 ELSE 0 END falla,
           CASE WHEN rep.id_lote IS NULL THEN 0 ELSE 1 END repetido
      FROM sal
      LEFT JOIN snap s ON s.id_producto = sal.id_producto AND s.id_bodega = sal.b
                      AND s.id_lote = sal.id_lote
      LEFT JOIN rep ON rep.b = sal.b AND rep.id_lote = sal.id_lote
)
SELECT x.doc, x.b AS bodega, MAX(o.numero) AS orden,
       CONVERT(varchar(16), MIN(x.fecha_creacion), 120) AS fecha,
       COUNT(*) AS lotes, SUM(x.falla) AS sin_bajar,
       CAST(SUM(CASE WHEN x.falla = 1 THEN x.cantidad ELSE 0 END) AS decimal(12, 2)) AS kg_sin_bajar,
       SUM(x.repetido) AS repetidos
  FROM x
  LEFT JOIN detalle_movimiento_inventario d ON d.id_detalle_movimiento_inventario = x.idm
  LEFT JOIN detalle_orden_salida_material dosm
         ON dosm.id_detalle_orden_salida_material = d.id_detalle_orden_salida_material
  LEFT JOIN orden_salida_material o ON o.id_orden_salida_material = dosm.id_orden_salida_material
 GROUP BY x.doc, x.b
HAVING SUM(x.falla) > 0 OR SUM(x.repetido) > 0
 ORDER BY MIN(x.fecha_creacion)
"""


def detectar(dias: int = 10) -> dict:
    """Salidas de los últimos `dias` con lotes que no bajaron o repetidos.

    Devuelve {"ok": bool (Asinfo contestó), "salidas": [...], "kg_sin_bajar",
    "lotes_sin_bajar", "lotes_repetidos"}. Nunca levanta.
    """
    try:
        from modules._lib import metabase_client
        filas, ok = metabase_client.fetch_dataset_estado(2, _sql(dias), max_results=5000)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("detectar falló: %s", e)
        return {"ok": False, "error": str(e)[:200], "salidas": []}
    if not ok:
        return {"ok": False, "error": "Asinfo no contestó", "salidas": []}
    salidas = []
    for f in filas or []:
        try:
            b = int(f.get("bodega"))
            salidas.append({
                "salida": str(f.get("doc") or "").strip(),
                "orden": str(f.get("orden") or "").strip(),
                "bodega": BODEGAS.get(b, str(b)),
                "fecha": str(f.get("fecha") or ""),
                "lotes": int(f.get("lotes") or 0),
                "sin_bajar": int(f.get("sin_bajar") or 0),
                "kg_sin_bajar": round(float(f.get("kg_sin_bajar") or 0), 2),
                "repetidos": int(f.get("repetidos") or 0),
            })
        except (TypeError, ValueError):
            continue
    return {
        "ok": True,
        "dias": dias,
        "salidas": salidas,
        "lotes_sin_bajar": sum(s["sin_bajar"] for s in salidas),
        "kg_sin_bajar": round(sum(s["kg_sin_bajar"] for s in salidas), 2),
        "lotes_repetidos": sum(s["repetidos"] for s in salidas),
    }


def _titulo(s: dict) -> str:
    partes = []
    if s["sin_bajar"]:
        partes.append(f"{s['sin_bajar']} de {s['lotes']} lotes no bajaron del saldo "
                      f"({s['kg_sin_bajar']:,.0f} kg)")
    if s["repetidos"]:
        partes.append(f"{s['repetidos']} lotes también están en otra salida")
    return f"Asinfo: {s['salida']} ({s['orden'] or 'sin orden'}, {s['bodega']}) — " + "; ".join(partes)


def health(dias: int = 10, avisar: bool = True) -> dict:
    """Para /admin/health/salidas-sin-saldo. Si Asinfo no contesta: ok con
    `sin_datos` (no es un problema contable). Si hay salidas con falla, alerta
    y deja un aviso en la campanita por cada salida (una sola vez por salida)."""
    res = detectar(dias)
    if not res.get("ok"):
        return {"ok": True, "alerts": [],
                "stats": {"sin_datos": True, "error": res.get("error")}}
    salidas = res["salidas"]
    alerts = []
    if salidas:
        ej = "; ".join(f"{s['salida']} {s['sin_bajar']}/{s['lotes']}" for s in salidas[:5])
        alerts.append({
            "severity": "high",
            "category": "salidas_sin_bajar_saldo",
            "msg": (f"{len(salidas)} salida(s) de material de los últimos {dias} días "
                    f"con lotes que Asinfo no bajó del saldo ({res['lotes_sin_bajar']} "
                    f"lotes, {res['kg_sin_bajar']:,.0f} kg) o cargados en dos salidas "
                    f"({res['lotes_repetidos']}): {ej}"
                    f"{'…' if len(salidas) > 5 else ''}. El stock y la utilidad "
                    f"están altos en esos kilos hasta que Asinfo lo corrija."),
        })
        if avisar:
            try:
                from modules.avisos import queries as avisos
                for s in salidas:
                    avisos.avisar(
                        fuente="stock", nivel="alerta",
                        titulo=_titulo(s)[:200],
                        detalle=("El lote quedó en la salida pero el saldo de la bodega "
                                 "en Asinfo no bajó: sigue figurando en stock. Pedirle a "
                                 "Asinfo que lo corrija."),
                        cantidad=int(s["kg_sin_bajar"]) or None,
                        clave=f"salida-sin-saldo:{s['salida']}",
                    )
            except Exception as e:  # noqa: BLE001 -- avisar nunca rompe el health
                _LOG.warning("no pude avisar: %s", e)
    return {"ok": not alerts, "alerts": alerts,
            "stats": {k: res[k] for k in ("dias", "lotes_sin_bajar", "kg_sin_bajar",
                                          "lotes_repetidos")}
                     | {"salidas": salidas}}
