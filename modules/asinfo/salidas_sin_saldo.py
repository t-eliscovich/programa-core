"""Salidas de material que Asinfo registró pero que NO bajaron el saldo.

Tamara 2026-10-01. Desde el 13/07/2026 (primer día hábil después de una
actualización de Asinfo del 10/07), a veces, escaneando lotes en una salida de
material (SM), el lote queda en la salida pero el saldo de la bodega no baja:
el lote sigue figurando en stock aunque ya se fue a producción. En el hilo
pasó en 58 de 1.034 salidas (jul–sep) y dejó ~52.000 kg de hilo "de más" en el
balance — ~124 mil de utilidad inflada sólo en septiembre. Nadie lo vio porque
el saldo equivocado es el del propio Asinfo.

Cómo se detecta: por cada lote de una SM de los últimos días, el saldo del
lote en `saldo_producto_lote` quedó MÁS ALTO que la suma de sus movimientos
(ingresos menos salidas): el saldo no bajó con la salida. También marca los
lotes de HILO cargados en dos o más salidas (las "etiquetas repetidas en dos
órdenes" que reportó Alex el 30/09). En tela cruda NO: ahí un rollo se reparte
entre dos órdenes seguidas todo el tiempo y es normal.

Además, el CUADRE de cada bodega (hilo, tela cruda, terminado): kilos según el
saldo contra kilos según los movimientos. Al 01/10: hilo +57.149 kg, tela
cruda +47.404 kg (ambas desde el 13/07), terminado ~0. Atrapa cualquier falla
del saldo, también una que todavía no conozcamos.

Corre solo cada 30 min desde el hilo de fondo (`correr_si_toca`) y además
cuando se abre /admin/health/salidas-sin-saldo o /admin/health/all.

Tamara 2026-10-01 (segundo caso): INGRESOS que Asinfo sumó DOS VECES al
saldo. En las importaciones del 11/09 (IM-0000591 e IM-0000607) 30 lotes de
hilo entraron una sola vez con 27 kg pero el saldo dice 54 (~817 kg de más).
Se detecta así: lotes con un ingreso de los últimos días, que todavía no
tuvieron ninguna salida, cuyo saldo es MAYOR que la suma de sus movimientos.
Medido el 01/10 sobre 60 días y las tres bodegas (51/52/53): sólo esos dos
ingresos, ningún falso positivo.

EL PATRÓN (medido el 01/10 sobre todas las salidas desde junio):

1. HILO — salidas cargadas lote por lote. Cuando la salida se escanea de a un
   bulto (una línea cada ~5 segundos) en vez de grabarse toda junta, desde el
   13/07 sólo el PRIMER bulto baja el saldo y el resto queda en la bodega. En
   ese modo fallan 1.559 de 1.795 lotes (87 %); grabadas juntas, el 1 %. Antes
   del 13/07 el mismo modo andaba bien (186 lotes, 0 fallas). Es stock de más
   de verdad: ~51.700 kg al 30/09.
2. TELA CRUDA — el mismo rollo en DOS salidas (las etiquetas repetidas en dos
   órdenes que reportó Alex). Antes del 13/07 pasó 2 veces; desde entonces,
   1.625 rollos. La segunda salida no baja nada porque el rollo ya está en 0:
   el saldo está BIEN y lo que se cuenta dos veces es la salida. No infla el
   stock (sí la orden que recibió el rollo dos veces). El stock de más de
   verdad en tela cruda son ~7.300 kg.

Por eso el cuadre separa la `diferencia` entera del `stock_de_mas`: sólo lo
segundo mueve la utilidad.

Sólo lee Asinfo (vía Metabase). Fail-soft: si Asinfo no contesta, no alarma.
"""
from __future__ import annotations

import logging

from modules._lib.medicion_reciente import MedicionReciente

_LOG = logging.getLogger("programa_core.asinfo.salidas_sin_saldo")

BODEGAS = {51: "Hilo", 52: "Tela cruda", 53: "Terminado"}


def _sql(dias: int) -> str:
    """Salidas de los últimos `dias` con lotes cuyo saldo quedó MÁS ALTO que la
    suma de sus movimientos (el saldo no bajó con la salida).

    TMT 2026-10-01 — antes se comparaba la FECHA del último saldo contra la de
    la salida, y eso se perdía los rollos de tela cruda repartidos en dos
    salidas el MISMO día (la primera baja, la segunda no). En tela cruda eran
    ~48.000 kg y el chequeo veía 5.900. Ahora se compara el saldo contra la
    suma de los movimientos del lote, que es exacto. La falla se le anota a la
    ÚLTIMA salida del lote (la que no bajó)."""
    dias = max(1, min(int(dias), 90))
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
lot AS (SELECT DISTINCT id_producto, b, id_lote FROM sal),
k AS (
    SELECT ip.id_producto, ip.id_bodega b, ip.id_lote, SUM(ip.operacion * ip.cantidad) kx
      FROM inventario_producto ip
      JOIN lot ON lot.id_producto = ip.id_producto AND lot.b = ip.id_bodega
              AND lot.id_lote = ip.id_lote
     WHERE ISNULL(ip.indicador_anulacion, 0) = 0
     GROUP BY ip.id_producto, ip.id_bodega, ip.id_lote
),
snap AS (
    SELECT s.id_producto, s.id_bodega, s.id_lote, s.saldo FROM (
        SELECT x.id_producto, x.id_bodega, x.id_lote, x.saldo,
               ROW_NUMBER() OVER (PARTITION BY x.id_producto, x.id_bodega, x.id_lote
                                  ORDER BY x.fecha DESC, x.id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote x
          JOIN lot ON lot.id_producto = x.id_producto AND lot.b = x.id_bodega
                  AND lot.id_lote = x.id_lote
    ) s WHERE rn = 1
),
dif AS (
    -- Stock DE MÁS de verdad: el saldo sigue positivo y es mayor que lo que
    -- dicen los movimientos. Un lote con saldo 0 y la salida contada dos veces
    -- (etiqueta repetida en dos órdenes) NO infla el stock: no es falla acá.
    SELECT k.id_producto, k.b, k.id_lote,
           CASE WHEN s.saldo < s.saldo - k.kx THEN s.saldo ELSE s.saldo - k.kx END de_mas
      FROM k JOIN snap s ON s.id_producto = k.id_producto AND s.id_bodega = k.b
                        AND s.id_lote = k.id_lote
     WHERE s.saldo - k.kx > 0.5 AND s.saldo > 0.5
),
ult AS (
    SELECT doc, b, id_producto, id_lote,
           ROW_NUMBER() OVER (PARTITION BY b, id_producto, id_lote
                              ORDER BY fecha_creacion DESC) rn
      FROM sal
),
rep AS (SELECT b, id_lote FROM sal WHERE b = 51
         GROUP BY b, id_lote HAVING COUNT(DISTINCT doc) > 1),
x AS (
    SELECT sal.*,
           CASE WHEN dif.id_lote IS NOT NULL AND u.rn = 1 THEN 1 ELSE 0 END falla,
           CASE WHEN dif.id_lote IS NOT NULL AND u.rn = 1 THEN dif.de_mas ELSE 0 END kg_falla,
           CASE WHEN rep.id_lote IS NULL THEN 0 ELSE 1 END repetido
      FROM sal
      JOIN ult u ON u.doc = sal.doc AND u.b = sal.b AND u.id_producto = sal.id_producto
                AND u.id_lote = sal.id_lote
      LEFT JOIN dif ON dif.id_producto = sal.id_producto AND dif.b = sal.b
                   AND dif.id_lote = sal.id_lote
      LEFT JOIN rep ON rep.b = sal.b AND rep.id_lote = sal.id_lote
)
SELECT x.doc, x.b AS bodega, MAX(o.numero) AS orden,
       CONVERT(varchar(16), MIN(x.fecha_creacion), 120) AS fecha,
       COUNT(*) AS lotes, SUM(x.falla) AS sin_bajar,
       CAST(SUM(x.kg_falla) AS decimal(12, 2)) AS kg_sin_bajar,
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


#: Stock de más que cada bodega YA tenía antes de la falla del 13/07/2026
#: (medido al 12/07): 0 en hilo y tela cruda, ~20 kg en terminado. La
#: diferencia vieja del hilo (2.296 kg de 2022–2023) es toda de lotes con
#: saldo 0: salidas contadas de más en los movimientos, no stock.
CUADRE_BASE_KG = {51: 0.0, 52: 0.0, 53: 30.0}
CUADRE_MARGEN_KG = 100.0

_SQL_CUADRE = """
WITH s AS (
    SELECT id_producto, id_bodega b, id_lote, saldo FROM (
        SELECT id_producto, id_bodega, id_lote, saldo,
               ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega, id_lote
                                  ORDER BY fecha DESC, id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote WHERE id_bodega IN (51, 52, 53)
    ) z WHERE rn = 1
),
k AS (
    SELECT id_producto, id_bodega b, id_lote, SUM(operacion * cantidad) kx
      FROM inventario_producto
     WHERE id_bodega IN (51, 52, 53) AND ISNULL(indicador_anulacion, 0) = 0
     GROUP BY id_producto, id_bodega, id_lote
),
j AS (
    SELECT COALESCE(s.b, k.b) b, ISNULL(s.saldo, 0) saldo, ISNULL(k.kx, 0) kx
      FROM s FULL JOIN k ON k.id_producto = s.id_producto AND k.b = s.b
                        AND k.id_lote = s.id_lote
)
SELECT b AS bodega, CAST(SUM(saldo) AS decimal(14, 2)) AS saldo,
       CAST(SUM(kx) AS decimal(14, 2)) AS movimientos,
       CAST(SUM(saldo - kx) AS decimal(14, 2)) AS diferencia,
       CAST(SUM(CASE WHEN saldo > 0.5 AND saldo - kx > 0.5
                     THEN CASE WHEN saldo < saldo - kx THEN saldo ELSE saldo - kx END
                     ELSE 0 END) AS decimal(14, 2)) AS stock_de_mas
  FROM j GROUP BY b ORDER BY b
"""


def cuadre() -> dict:
    """El cuadre de cada bodega: kilos según el saldo de Asinfo contra kilos
    según sus ingresos y salidas. `stock_de_mas` es la parte que infla el
    stock: lotes cuyo saldo sigue positivo y por encima de sus movimientos.
    Atrapa CUALQUIER falla del saldo, también una que todavía no conocemos.
    Nunca levanta."""
    try:
        from modules._lib import metabase_client
        filas, ok = metabase_client.fetch_dataset_estado(2, _SQL_CUADRE, max_results=10)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("cuadre falló: %s", e)
        return {"ok": False, "error": str(e)[:200], "bodegas": []}
    if not ok:
        return {"ok": False, "error": "Asinfo no contestó", "bodegas": []}
    bodegas = []
    for f in filas or []:
        try:
            b = int(f.get("bodega"))
            dif = round(float(f.get("diferencia") or 0), 2)
            de_mas = round(float(f.get("stock_de_mas") or 0), 2)
            base = CUADRE_BASE_KG.get(b, 0.0)
            bodegas.append({
                "id": b,
                "bodega": BODEGAS.get(b, str(b)),
                "saldo": round(float(f.get("saldo") or 0), 2),
                "movimientos": round(float(f.get("movimientos") or 0), 2),
                "diferencia": dif,
                # Lo que de verdad infla el stock (y la utilidad): lotes con
                # saldo positivo más alto que sus movimientos. El resto de la
                # diferencia son salidas contadas dos veces con saldo 0.
                "stock_de_mas": de_mas,
                "de_mas_nuevo": round(de_mas - base, 2),
                "descuadrada": (de_mas - base) > CUADRE_MARGEN_KG,
            })
        except (TypeError, ValueError):
            continue
    return {"ok": True, "bodegas": bodegas}


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


def _sql_docs(docs: list[str]) -> str:
    """De estas salidas, cuáles SIGUEN con lotes de más en el saldo (sin
    importar la fecha: sirve para ver si Asinfo arregló una salida vieja)."""
    limpios = sorted({d for d in docs if d and all(c.isalnum() or c in "-/" for c in d)})
    lista = ", ".join(f"'{d}'" for d in limpios) or "''"
    return f"""
WITH sal AS (
    SELECT DISTINCT ip.numero_documento doc, ip.id_bodega b, ip.id_producto, ip.id_lote
      FROM inventario_producto ip
     WHERE ip.numero_documento IN ({lista}) AND ip.operacion = -1
       AND ISNULL(ip.indicador_anulacion, 0) = 0
),
lot AS (SELECT DISTINCT id_producto, b, id_lote FROM sal),
k AS (
    SELECT ip.id_producto, ip.id_bodega b, ip.id_lote, SUM(ip.operacion * ip.cantidad) kx
      FROM inventario_producto ip
      JOIN lot ON lot.id_producto = ip.id_producto AND lot.b = ip.id_bodega
              AND lot.id_lote = ip.id_lote
     WHERE ISNULL(ip.indicador_anulacion, 0) = 0
     GROUP BY ip.id_producto, ip.id_bodega, ip.id_lote
),
snap AS (
    SELECT s.id_producto, s.id_bodega, s.id_lote, s.saldo FROM (
        SELECT x.id_producto, x.id_bodega, x.id_lote, x.saldo,
               ROW_NUMBER() OVER (PARTITION BY x.id_producto, x.id_bodega, x.id_lote
                                  ORDER BY x.fecha DESC, x.id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote x
          JOIN lot ON lot.id_producto = x.id_producto AND lot.b = x.id_bodega
                  AND lot.id_lote = x.id_lote
    ) s WHERE rn = 1
)
SELECT sal.doc, COUNT(*) AS lotes
  FROM sal
  JOIN k ON k.id_producto = sal.id_producto AND k.b = sal.b AND k.id_lote = sal.id_lote
  JOIN snap s ON s.id_producto = sal.id_producto AND s.id_bodega = sal.b
             AND s.id_lote = sal.id_lote
 WHERE s.saldo > 0.5 AND s.saldo - k.kx > 0.5
 GROUP BY sal.doc
"""


def avisar_arreglos(res: dict, ing: dict, cua: dict) -> list[str]:
    """Cuando Asinfo ARREGLA algo, el aviso de la campanita se da vuelta a
    "resuelto" (vuelve a no leído para que se vea). Tamara 2026-10-01: "también
    me podés avisar si algo arreglan?".

    · Salidas: se vuelve a mirar CADA salida con aviso abierto, sea de la fecha
      que sea; si ya no le queda ningún lote de más, se resolvió.
    · Ingresos sumados dos veces: si ya no aparece entre los de los últimos
      90 días.
    · Bodega: si el stock de más bajó un escalón de 1.000 kg (o a cero), se
      resuelven los avisos de los escalones de arriba.
    Sólo con Asinfo contestando; nunca levanta. Devuelve las claves resueltas.
    """
    hechas: list[str] = []
    try:
        from modules._lib import metabase_client
        from modules.avisos import queries as avisos

        abiertas = avisos.abiertos_por_clave("salida-sin-saldo:")
        if abiertas and res.get("ok"):
            docs = [a["clave"].split(":", 1)[1] for a in abiertas]
            filas, ok = metabase_client.fetch_dataset_estado(
                2, _sql_docs(docs), max_results=5000)
            if ok:
                siguen = {str(f.get("doc") or "").strip() for f in filas or []}
                for a in abiertas:
                    doc = a["clave"].split(":", 1)[1]
                    if doc not in siguen:
                        avisos.resolver(
                            int(a["id_aviso"]),
                            titulo=f"Asinfo arregló {doc}: los lotes ya bajaron del saldo",
                            detalle="El saldo de la bodega ya no cuenta esos lotes. "
                                    "El stock y la utilidad bajan en esos kilos.")
                        hechas.append(a["clave"])

        abiertas = avisos.abiertos_por_clave("ingreso-doble:")
        if abiertas and ing.get("ok"):
            todavia = detectar_ingresos(90)
            if todavia.get("ok"):
                siguen = {i["ingreso"] for i in todavia.get("ingresos") or []}
                for a in abiertas:
                    doc = a["clave"].split(":", 1)[1]
                    if doc not in siguen:
                        avisos.resolver(
                            int(a["id_aviso"]),
                            titulo=f"Asinfo arregló {doc}: el saldo ya no lo cuenta dos veces",
                            detalle="Los lotes quedaron con los kilos que entraron.")
                        hechas.append(a["clave"])

        if cua.get("ok"):
            # Los avisos viejos del cuadre (antes del 01/10 medían la diferencia
            # entera, que mezcla salidas contadas dos veces con saldo 0) se
            # archivan sin decir "arreglado": no se arregló nada.
            for a in avisos.abiertos_por_clave("cuadre:"):
                avisos.archivar(int(a["id_aviso"]), "salidas-sin-saldo")
            por_id = {b["id"]: b for b in cua.get("bodegas") or []}
            for a in avisos.abiertos_por_clave("stock-de-mas:"):
                try:
                    _, b_id, paso = a["clave"].split(":")
                    b = por_id.get(int(b_id))
                    if b is None:
                        continue
                    hoy = int(b["de_mas_nuevo"] // 1000) if b["descuadrada"] else -1
                    if hoy < int(paso):
                        if b["descuadrada"]:
                            tit = (f"Asinfo corrigió parte: la bodega de "
                                   f"{b['bodega'].lower()} bajó a "
                                   f"{b['de_mas_nuevo']:,.0f} kg de más")
                        else:
                            tit = (f"Asinfo lo arregló: la bodega de "
                                   f"{b['bodega'].lower()} ya no tiene kilos de más")
                        avisos.resolver(int(a["id_aviso"]), titulo=tit[:200],
                                        detalle="El stock y la utilidad bajan en "
                                                "los kilos que se corrigieron.")
                        hechas.append(a["clave"])
                except (ValueError, KeyError, TypeError):
                    continue
    except Exception as e:  # noqa: BLE001 -- nunca rompe el health
        _LOG.warning("avisar_arreglos: %s", e)
    if hechas:
        olvidar_lo_calculado()
    return hechas


def olvidar_lo_calculado() -> None:
    """Asinfo corrigió algo: el Flujo de producción y su "Mes a mes" tienen
    guardado lo de antes (los meses cerrados, 12 h). Tamara 02/10/2026: el
    saldo ya estaba bien y la pantalla seguía mostrando 51.740 kg de más."""
    try:
        from modules.asinfo import kardex_bodegas
        kardex_bodegas.reset_cache()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude vaciar el mes a mes: %s", e)
    try:
        from modules.informes.views import reset_flujo_produccion_cache
        reset_flujo_produccion_cache()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude vaciar el flujo de producción: %s", e)


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
    cua = cuadre()
    if not res.get("ok") and not ing.get("ok") and not cua.get("ok"):
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
    descuadradas = [b for b in cua.get("bodegas") or [] if b["descuadrada"]]
    if descuadradas:
        det = "; ".join(f"{b['bodega']}: {b['de_mas_nuevo']:,.0f} kg de más en el saldo"
                        for b in descuadradas)
        alerts.append({
            "severity": "high",
            "category": "bodega_descuadrada",
            "msg": (f"El saldo de Asinfo tiene lotes con más kilos de los que dicen sus "
                    f"ingresos y salidas — {det}. Esos kilos están de más en el stock "
                    f"y en la utilidad."),
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
            for b in descuadradas:
                # Un aviso nuevo cada 1.000 kg que se agranda: si sigue
                # creciendo, la campanita lo vuelve a decir.
                paso = int(b["de_mas_nuevo"] // 1000)
                avisos.avisar(
                    fuente="stock", nivel="alerta",
                    titulo=(f"Asinfo: la bodega de {b['bodega'].lower()} tiene "
                            f"{b['de_mas_nuevo']:,.0f} kg de más en el saldo")[:200],
                    detalle=("Lotes que salieron pero el saldo de Asinfo los sigue "
                             "mostrando en la bodega. Inflan el stock y la utilidad. "
                             "Pedirle a Asinfo que lo corrija."),
                    cantidad=int(b["de_mas_nuevo"]) or None,
                    clave=f"stock-de-mas:{b['id']}:{paso}",
                )
        except Exception as e:  # noqa: BLE001 -- avisar nunca rompe el health
            _LOG.warning("no pude avisar: %s", e)
    if avisar:
        avisar_arreglos(res, ing, cua)
    stats = {k: res.get(k) for k in ("dias", "lotes_sin_bajar", "kg_sin_bajar",
                                     "lotes_repetidos")}
    stats |= {"salidas": salidas,
              "ingresos": ingresos,
              "lotes_ingreso_de_mas": ing.get("lotes", 0),
              "kg_ingreso_de_mas": ing.get("kg_de_mas", 0),
              "cuadre": cua.get("bodegas") or []}
    out = {"ok": not alerts, "alerts": alerts, "stats": stats}
    ULTIMA.guardar(out)
    return out


#: Lo último que midió `health()` (el hilo de fondo lo corre cada 30 min).
ULTIMA = MedicionReciente()


def health_reciente(max_edad_secs: int = 3600) -> dict:
    """Para /admin/health/all: lo que midió el hilo de fondo si tiene menos de
    una hora; si no, mide ahora. Tamara 2026-10-07: medir de nuevo acá tardaba
    ~48 s y era la mitad de por qué el health/all "se colgaba"."""
    return ULTIMA.leer(max_edad_secs) or health()


#: Cada cuánto corre solo el control (hilo de fondo). Default 30 min, todo el
#: día: las salidas se escanean en los tres turnos.
_INTERVALO_SECS = 1800
_auto_ultimo: float | None = None


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo (autocarga_facturas). Corre el control completo
    y deja los avisos, como mucho una vez cada `SALIDAS_SALDO_SECS` (default
    30 min). SALIDAS_SALDO_AUTO=0 lo apaga. Nunca levanta.

    Tamara 2026-10-01: "este control debe ser continuo" — antes sólo corría
    cuando alguien abría el health."""
    import os
    import time as _time
    global _auto_ultimo
    res: dict = {"corrio": False}
    if os.environ.get("SALIDAS_SALDO_AUTO", "1") == "0":
        return res
    try:
        intervalo = max(300, int(os.environ.get("SALIDAS_SALDO_SECS", _INTERVALO_SECS)))
    except ValueError:
        intervalo = _INTERVALO_SECS
    ahora = _time.monotonic()
    if _auto_ultimo is not None and (ahora - _auto_ultimo) < intervalo:
        return res
    _auto_ultimo = ahora
    try:
        h = health(avisar=True)
        res.update(corrio=True, ok=h.get("ok"),
                   alertas=[a["category"] for a in h.get("alerts") or []])
        # Y de paso deja listo el "Mes a mes" del Flujo de producción, que
        # tarda en armarse contra Asinfo (Tamara 2026-10-02).
        from modules.asinfo import kardex_bodegas
        kardex_bodegas.calentar()
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("control de salidas y saldo (fondo): %s", e)
    return res
