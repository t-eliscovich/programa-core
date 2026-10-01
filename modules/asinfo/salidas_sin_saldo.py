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

Tamara 2026-10-01 (segundo caso): INGRESOS que Asinfo sumó DOS VECES al
saldo. En las importaciones del 11/09 (IM-0000591 e IM-0000607) 30 lotes de
hilo entraron una sola vez con 27 kg pero el saldo dice 54 (~817 kg de más).
Se detecta así: lotes con un ingreso de los últimos días, que todavía no
tuvieron ninguna salida, cuyo saldo es MAYOR que la suma de sus movimientos.
Medido el 01/10 sobre 60 días y las tres bodegas (51/52/53): sólo esos dos
ingresos, ningún falso positivo.

Sólo lee Asinfo (vía Metabase). Fail-soft: si Asinfo no contesta, no alarma.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("programa_core.asinfo.salidas_sin_saldo")

BODEGAS = {51: "Hilo", 52: "Tela cruda", 53: "Terminado"}


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


def _sql_ingresos(dias: int) -> str:
    """Ingresos de los últimos `dias` cuyo lote tiene en el saldo MÁS kilos que
    la suma de sus movimientos, sin haber tenido ninguna salida todavía."""
    dias = max(1, min(int(dias), 90))
    return f"""
WITH ing AS (
    SELECT ip.numero_documento doc, ip.id_bodega b, ip.id_producto, ip.id_lote,
           ip.fecha_creacion
      FROM inventario_producto ip
     WHERE ip.id_bodega IN (51, 52, 53) AND ip.operacion = 1
       AND ISNULL(ip.indicador_anulacion, 0) = 0
       AND ip.fecha >= DATEADD(day, -{dias}, CAST(GETDATE() AS date))
       AND ip.fecha_creacion < DATEADD(minute, -15, GETDATE())
),
k AS (
    SELECT ip.id_producto, ip.id_bodega b, ip.id_lote,
           SUM(ip.operacion * ip.cantidad) kx,
           SUM(CASE WHEN ip.operacion = -1 THEN 1 ELSE 0 END) nsal
      FROM inventario_producto ip
      JOIN (SELECT DISTINCT id_producto, b, id_lote FROM ing) z
        ON z.id_producto = ip.id_producto AND z.b = ip.id_bodega AND z.id_lote = ip.id_lote
     WHERE ISNULL(ip.indicador_anulacion, 0) = 0
     GROUP BY ip.id_producto, ip.id_bodega, ip.id_lote
),
snap AS (
    SELECT s.id_producto, s.id_bodega, s.id_lote, s.saldo FROM (
        SELECT x.id_producto, x.id_bodega, x.id_lote, x.saldo,
               ROW_NUMBER() OVER (PARTITION BY x.id_producto, x.id_bodega, x.id_lote
                                  ORDER BY x.fecha DESC, x.id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote x
          JOIN (SELECT DISTINCT id_producto, b, id_lote FROM ing) z
            ON z.id_producto = x.id_producto AND z.b = x.id_bodega AND z.id_lote = x.id_lote
    ) s WHERE rn = 1
),
x AS (
    SELECT ing.doc, ing.b, ing.fecha_creacion, s.saldo - k.kx de_mas
      FROM ing
      JOIN k ON k.id_producto = ing.id_producto AND k.b = ing.b AND k.id_lote = ing.id_lote
      JOIN snap s ON s.id_producto = ing.id_producto AND s.id_bodega = ing.b
                 AND s.id_lote = ing.id_lote
     WHERE k.nsal = 0 AND s.saldo - k.kx > 0.5
)
SELECT x.doc, x.b AS bodega, CONVERT(varchar(16), MIN(x.fecha_creacion), 120) AS fecha,
       COUNT(*) AS lotes, CAST(SUM(x.de_mas) AS decimal(12, 2)) AS kg_de_mas
  FROM x
 GROUP BY x.doc, x.b
 ORDER BY MIN(x.fecha_creacion)
"""


def detectar_ingresos(dias: int = 30) -> dict:
    """Ingresos de los últimos `dias` que Asinfo sumó de más al saldo.

    Devuelve {"ok": bool (Asinfo contestó), "ingresos": [...], "lotes",
    "kg_de_mas"}. Nunca levanta.
    """
    try:
        from modules._lib import metabase_client
        filas, ok = metabase_client.fetch_dataset_estado(
            2, _sql_ingresos(dias), max_results=5000)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("detectar_ingresos falló: %s", e)
        return {"ok": False, "error": str(e)[:200], "ingresos": []}
    if not ok:
        return {"ok": False, "error": "Asinfo no contestó", "ingresos": []}
    ingresos = []
    for f in filas or []:
        try:
            b = int(f.get("bodega"))
            ingresos.append({
                "ingreso": str(f.get("doc") or "").strip(),
                "bodega": BODEGAS.get(b, str(b)),
                "fecha": str(f.get("fecha") or ""),
                "lotes": int(f.get("lotes") or 0),
                "kg_de_mas": round(float(f.get("kg_de_mas") or 0), 2),
            })
        except (TypeError, ValueError):
            continue
    return {
        "ok": True,
        "dias": dias,
        "ingresos": ingresos,
        "lotes": sum(i["lotes"] for i in ingresos),
        "kg_de_mas": round(sum(i["kg_de_mas"] for i in ingresos), 2),
    }


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


def health(dias: int = 10, avisar: bool = True, dias_ingresos: int = 30) -> dict:
    """Para /admin/health/salidas-sin-saldo. Dos chequeos contra Asinfo:
    salidas que no bajaron el saldo (o lotes en dos salidas) e ingresos que
    el saldo sumó dos veces. Si Asinfo no contesta: ok con `sin_datos` (no es
    un problema contable). Si hay fallas, alerta y deja un aviso en la
    campanita por cada documento (una sola vez por documento)."""
    res = detectar(dias)
    ing = detectar_ingresos(dias_ingresos)
    if not res.get("ok") and not ing.get("ok"):
        return {"ok": True, "alerts": [],
                "stats": {"sin_datos": True, "error": res.get("error")}}
    salidas = res.get("salidas") or []
    ingresos = ing.get("ingresos") or []
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
    if ingresos:
        ej = "; ".join(f"{i['ingreso']} {i['lotes']} lotes" for i in ingresos[:5])
        alerts.append({
            "severity": "high",
            "category": "ingresos_sumados_dos_veces",
            "msg": (f"{len(ingresos)} ingreso(s) de los últimos {dias_ingresos} días "
                    f"que Asinfo sumó de más al saldo ({ing['lotes']} lotes, "
                    f"{ing['kg_de_mas']:,.0f} kg): {ej}"
                    f"{'…' if len(ingresos) > 5 else ''}. El lote entró una vez pero "
                    f"el saldo tiene más kilos. El stock y la utilidad están altos en "
                    f"esos kilos hasta que Asinfo lo corrija."),
        })
    if avisar and alerts:
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
            for i in ingresos:
                avisos.avisar(
                    fuente="stock", nivel="alerta",
                    titulo=(f"Asinfo: {i['ingreso']} ({i['bodega']}) — {i['lotes']} "
                            f"lotes con más kilos en el saldo de los que entraron "
                            f"({i['kg_de_mas']:,.0f} kg de más)")[:200],
                    detalle=("El lote entró una sola vez pero el saldo de la bodega en "
                             "Asinfo lo cuenta de más (por ejemplo, entró con 27 kg y "
                             "el saldo dice 54). Pedirle a Asinfo que lo corrija."),
                    cantidad=int(i["kg_de_mas"]) or None,
                    clave=f"ingreso-doble:{i['ingreso']}",
                )
        except Exception as e:  # noqa: BLE001 -- avisar nunca rompe el health
            _LOG.warning("no pude avisar: %s", e)
    stats = {k: res.get(k) for k in ("dias", "lotes_sin_bajar", "kg_sin_bajar",
                                     "lotes_repetidos")}
    stats |= {"salidas": salidas,
              "ingresos": ingresos,
              "lotes_ingreso_de_mas": ing.get("lotes", 0),
              "kg_ingreso_de_mas": ing.get("kg_de_mas", 0)}
    return {"ok": not alerts, "alerts": alerts, "stats": stats}
