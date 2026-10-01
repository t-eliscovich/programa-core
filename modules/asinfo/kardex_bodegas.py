"""Movimientos de las bodegas 51/52/53 de Asinfo MES POR MES, con sus documentos.

Tamara 2026-10-01. El cuadro de movimientos del Flujo de producción tomaba el
stock final del saldo VIVO de Asinfo aun mirando un mes pasado (agosto mostraba
el stock de hoy). Y el saldo de Asinfo cuenta kilos de más desde el 13/07 (ver
`salidas_sin_saldo`). Para un mes cerrado la cuenta sale de los MOVIMIENTOS
(`inventario_producto`), que cuadran al kilo con la planilla de Tamara:

    inicial + ingresos + ajustes − salidas = final según movimientos

    · Hilo (51):       ingresos = BOD (recepción de compras e importaciones),
                       salidas = SM (salida a tejeduría).
    · Tela cruda (52): ingresos = IFT (ingreso de fabricación), salidas = SM.
    · Terminado (53):  ingresos = IFT, salidas = DES (despacho).
    · Todo lo demás (AING, AEGR, TFB, NCNT, ...) va a "ajustes", con su signo.

Al lado va el saldo de Asinfo al cierre del mes: la diferencia son los kilos
que el saldo cuenta de más (o de menos). Está a la vista para agarrar estos
errores hasta que Asinfo los arregle.

Sólo lee Asinfo (Metabase). Fail-soft: {"ok": False} si no contesta.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

_LOG = logging.getLogger("programa_core.asinfo.kardex_bodegas")

BODEGAS = (51, 52, 53)
INGRESO = {51: "BOD", 52: "IFT", 53: "IFT"}
SALIDA = {51: "SM", 52: "SM", 53: "DES"}

#: Un mes cerrado casi no cambia: 30 min. El mes en curso, 5 min.
TTL_CERRADO = 1800
TTL_EN_CURSO = 300
_CACHE: dict = {}
_LOCK = threading.Lock()


def _ym(d: date) -> int:
    return d.year * 100 + d.month


def _siguiente(anio: int, mes: int) -> date:
    return date(anio + 1, 1, 1) if mes == 12 else date(anio, mes + 1, 1)


def _cache_get(clave):
    with _LOCK:
        hit = _CACHE.get(clave)
    if hit and time.time() < hit[0]:
        return hit[1]
    return None


def _cache_put(clave, valor, ttl):
    with _LOCK:
        _CACHE[clave] = (time.time() + ttl, valor)


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _sql_movimientos(desde: date, hasta: date) -> str:
    """Kilos por bodega × mes × prefijo del documento × operación, desde
    `desde` (incluido) hasta `hasta` (excluido). Lo anterior a `desde` se
    suma en el balde 0 = el inicial."""
    d1, d2 = desde.isoformat(), hasta.isoformat()
    return f"""
SELECT id_bodega AS b,
       CASE WHEN fecha < '{d1}' THEN 0 ELSE YEAR(fecha) * 100 + MONTH(fecha) END AS ym,
       CASE WHEN fecha < '{d1}' THEN ''
            ELSE LEFT(numero_documento, CHARINDEX('-', numero_documento + '-') - 1) END AS pre,
       operacion AS op,
       CAST(SUM(cantidad) AS decimal(16, 3)) AS kg
  FROM inventario_producto
 WHERE id_bodega IN (51, 52, 53)
   AND ISNULL(indicador_anulacion, 0) = 0
   AND fecha < '{d2}'
 GROUP BY id_bodega,
          CASE WHEN fecha < '{d1}' THEN 0 ELSE YEAR(fecha) * 100 + MONTH(fecha) END,
          CASE WHEN fecha < '{d1}' THEN ''
               ELSE LEFT(numero_documento, CHARINDEX('-', numero_documento + '-') - 1) END,
          operacion
"""


def _sql_saldo(hasta: date) -> str:
    """Saldo de Asinfo por bodega al cierre: la última foto de cada lote con
    fecha ANTERIOR a `hasta`."""
    return f"""
SELECT id_bodega AS b, CAST(SUM(saldo) AS decimal(16, 3)) AS saldo
  FROM (SELECT id_bodega, saldo,
               ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega, id_lote
                                  ORDER BY fecha DESC, id_saldo_producto_lote DESC) rn
          FROM saldo_producto_lote
         WHERE id_bodega IN (51, 52, 53) AND fecha < '{hasta.isoformat()}') z
 WHERE rn = 1
 GROUP BY id_bodega
"""


def _consultar(sql: str, max_results: int = 5000):
    from modules._lib import metabase_client
    return metabase_client.fetch_dataset_estado(2, sql, max_results=max_results)


def _saldo_al(hasta: date, en_curso: bool) -> dict | None:
    clave = ("saldo", hasta)
    hit = _cache_get(clave)
    if hit is not None:
        return hit
    try:
        filas, ok = _consultar(_sql_saldo(hasta), max_results=10)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("saldo al %s falló: %s", hasta, e)
        return None
    if not ok:
        return None
    out = {int(f["b"]): float(f.get("saldo") or 0) for f in filas or []}
    _cache_put(clave, out, TTL_EN_CURSO if en_curso else TTL_CERRADO)
    return out


def _armar(filas, meses: list[tuple[int, int]]) -> dict:
    """De las filas crudas a {(anio, mes): {bodega: {...}}} encadenando el
    inicial de cada mes con el final del anterior."""
    ini = {b: 0.0 for b in BODEGAS}
    por_mes: dict = {}
    for f in filas or []:
        b, ym = int(f["b"]), int(f["ym"])
        kg = float(f.get("kg") or 0) * int(f["op"])
        if ym == 0:
            ini[b] = ini.get(b, 0.0) + kg
            continue
        d = por_mes.setdefault(ym, {}).setdefault(b, {"ingresos": 0.0, "salidas": 0.0,
                                                      "ajustes": 0.0, "docs": {}})
        pre = str(f.get("pre") or "").strip().upper()
        if kg > 0 and pre == INGRESO[b]:
            d["ingresos"] += kg
        elif kg < 0 and pre == SALIDA[b]:
            d["salidas"] += -kg
        else:
            d["ajustes"] += kg
        d["docs"][pre] = d["docs"].get(pre, 0.0) + kg
    out = {}
    corriente = dict(ini)
    for (a, m) in meses:
        fila = {}
        for b in BODEGAS:
            d = por_mes.get(a * 100 + m, {}).get(b) or {"ingresos": 0.0, "salidas": 0.0,
                                                          "ajustes": 0.0, "docs": {}}
            inicial = corriente[b]
            final = inicial + d["ingresos"] + d["ajustes"] - d["salidas"]
            fila[b] = {"inicial": inicial, "ingresos": d["ingresos"],
                       "ajustes": d["ajustes"], "salidas": d["salidas"],
                       "final": final, "docs": d["docs"]}
            corriente[b] = final
        out[(a, m)] = fila
    return out


def meses(anio: int, desde_mes: int, hasta_mes: int, hoy: date | None = None) -> dict:
    """Movimientos de cada mes de `anio` entre `desde_mes` y `hasta_mes`
    (incluidos), con el saldo de Asinfo al cierre de cada uno.

    Devuelve {"ok": bool, "meses": [{"anio", "mes", "en_curso",
    51: {inicial, ingresos, ajustes, salidas, final, saldo, de_mas, docs}, 52:…, 53:…}]}.
    En el mes en curso el saldo es el de hoy. Nunca levanta.
    """
    if hoy is None:
        from filters import today_ec
        hoy = today_ec()
    desde_mes = max(1, min(int(desde_mes), 12))
    hasta_mes = max(desde_mes, min(int(hasta_mes), 12))
    lista = [(int(anio), m) for m in range(desde_mes, hasta_mes + 1)]
    return _meses_lista(lista, hoy)


def ultimos(anio: int, mes_: int, n: int = 5, hoy: date | None = None) -> dict:
    """Los `n` meses que terminan en (anio, mes_), cruzando de año si hace
    falta. Mismo formato que `meses`. Tamara 2026-10-01: el mes a mes con los
    últimos 5, no el año entero."""
    if hoy is None:
        from filters import today_ec
        hoy = today_ec()
    a, m = int(anio), max(1, min(int(mes_), 12))
    lista = []
    for _ in range(max(1, int(n))):
        lista.append((a, m))
        a, m = (a - 1, 12) if m == 1 else (a, m - 1)
    return _meses_lista(list(reversed(lista)), hoy)


def _meses_lista(lista: list[tuple[int, int]], hoy: date) -> dict:
    lista = [x for x in lista if date(x[0], x[1], 1) <= hoy]
    if not lista:
        return {"ok": True, "meses": []}
    desde = date(lista[0][0], lista[0][1], 1)
    hasta = _siguiente(*lista[-1])
    en_curso_algo = hasta > hoy

    clave = ("mov", desde, hasta)
    filas = _cache_get(clave)
    if filas is None:
        try:
            filas, ok = _consultar(_sql_movimientos(desde, hasta))
        except Exception as e:  # noqa: BLE001
            _LOG.warning("movimientos %s–%s falló: %s", desde, hasta, e)
            return {"ok": False, "meses": []}
        if not ok:
            return {"ok": False, "meses": []}
        _cache_put(clave, filas, TTL_EN_CURSO if en_curso_algo else TTL_CERRADO)

    armado = _armar(filas, lista)

    # Saldos al cierre de cada mes: de a tres consultas a la vez (Metabase se
    # ahoga con más — ver warmup._PASOS_A_LA_VEZ).
    cortes = [(x, _siguiente(*x)) for x in lista]
    with ThreadPoolExecutor(max_workers=3) as ex:
        saldos = list(ex.map(lambda c: _saldo_al(c[1], c[1] > hoy), cortes))

    salida = []
    prev_de_mas = {b: None for b in BODEGAS}
    for (x, _corte), saldo in zip(cortes, saldos, strict=False):
        fila = {"anio": x[0], "mes": x[1], "en_curso": _corte > hoy}
        for b in BODEGAS:
            d = dict(armado[x][b])
            s = None if saldo is None else float(saldo.get(b, 0.0))
            d["saldo"] = s
            d["de_mas"] = None if s is None else s - d["final"]
            d["de_mas_nuevo"] = (None if d["de_mas"] is None or prev_de_mas[b] is None
                                 else d["de_mas"] - prev_de_mas[b])
            prev_de_mas[b] = d["de_mas"]
            fila[b] = d
        salida.append(fila)
    return {"ok": True, "meses": salida}


def mes(anio: int, mes_: int, hoy: date | None = None) -> dict | None:
    """Un solo mes (lo que usa el cuadro de movimientos). None si Asinfo no
    contestó."""
    r = meses(anio, mes_, mes_, hoy=hoy)
    if not r.get("ok") or not r.get("meses"):
        return None
    return r["meses"][0]
