"""Seguimiento de las órdenes de tintura: producto, cliente, pedido y avance.

Tamara 2026-10-01: *"este dato de cuántos kg/total ponerlo en una pantalla de
Programa Core. Una pantalla única donde aparece producto, cliente, pedido,
estado"*. El dato que mostró era el de formulas: "26-09-783 · OFT-000042155 —
138/156 kg".

De dónde sale cada cosa:

    · La ORDEN de tintura (número, OFT y pedido) → formulas_app `ordenes`.
    · Producto y kilos del plan → la foto de la OFT que guarda formulas
      (`oft_snapshot`), con el plan de Asinfo si está.
    · Kilos FABRICADOS y estado → Asinfo `orden_fabricacion` (la OFT y sus
      hijas): es el mismo "138/156" que muestra formulas.
    · Cliente → Asinfo `pedido_cliente` + `empresa` (código y nombre).

Estados PARA LA BODEGA (Tamara 2026-10-01: *"no es lo mismo que termine
porque cierra en tintorería, ésta sería para la bodega"*):

    · En tintura — todavía no entró ningún kilo a la bodega de terminado.
      Se dice hace cuántos días se lanzó (más de 10 días, en naranja).
    · Parcial    — entró una parte y la orden sigue abierta (menos del 80%).
    · En bodega  — ya entró. El ingreso llega casi siempre de una sola vez.

Medido el 01/10/2026 sobre 900 órdenes de septiembre: las cerradas llegan al
87–91% del plan (mediana 89%) — el plan lleva la merma de tintura y lo que no
entró a la máquina, así que "no llegó al plan" es lo normal y NO se marca.
Una orden sin pedido es producción para stock.

Sólo lee. Fail-soft: sin formulas no hay lista; sin Asinfo se muestra la lista
sin avance ni cliente.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta

_LOG = logging.getLogger("programa_core.pedidos.ordenes")

ASINFO_DB = 2
ESTADO_FINALIZADA = 5
DIAS_DEFAULT = 30
#: La pantalla de la bodega se refresca cada 5 min; la lectura en frío tarda.
TTL = 600
_CACHE: dict = {}
_LOCK = threading.Lock()

ESTADOS = (
    ("en_tintura", "En tintura"),
    ("parcial", "Parcial"),
    ("en_bodega", "En bodega"),
)
#: Debajo de esto, una orden abierta con kilos es "Parcial".
PARCIAL_HASTA = 0.80
#: Más días que esto en tintura sin entrar nada a bodega, se marca.
DIAS_DEMORA = 10


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _seguro(s: str) -> str:
    """Sólo letras, números, guion y punto: va interpolado en un IN."""
    return "".join(c for c in (s or "").strip().upper() if c.isalnum() or c in "-.")[:30]


def _ordenes_formulas(desde: date) -> list[dict]:
    from modules._lib import formulas_db
    return formulas_db.fetch_all(
        """
        SELECT numero, codigo, fecha, oft_numero, pedido_numero,
               oft_snapshot->>'cantidad' AS plan_kg,
               COALESCE(oft_snapshot->>'producto_nombre',
                        oft_snapshot->'subordenes'->0->>'producto_nombre') AS producto,
               COALESCE(oft_snapshot->>'producto_codigo',
                        oft_snapshot->'subordenes'->0->>'producto_codigo') AS producto_codigo
          FROM ordenes
         WHERE oft_numero IS NOT NULL AND oft_numero <> ''
           AND created_at >= %s
         ORDER BY id DESC
        """,
        (desde.isoformat(),),
    ) or []


def _ofts_asinfo(ofts: list[str]) -> tuple[dict, bool]:
    if not ofts:
        return {}, True
    from modules._lib import metabase_client
    in_list = ", ".join(f"'{o}'" for o in ofts)
    # Sin OR en el JOIN (tardaba 15 s): primero las OFT, después la suma de
    # sus hijas agrupada por padre (8 s medido el 01/10/2026).
    sql = f"""
WITH p AS (
  SELECT id_orden_fabricacion, numero, estado_produccion, cantidad, cantidad_fabricada
    FROM orden_fabricacion
   WHERE numero IN ({in_list})
), h AS (
  SELECT o.id_orden_fabricacion_padre AS padre,
         SUM(ISNULL(o.cantidad_fabricada, 0)) AS fab
    FROM orden_fabricacion o
    JOIN p ON p.id_orden_fabricacion = o.id_orden_fabricacion_padre
   GROUP BY o.id_orden_fabricacion_padre
)
SELECT p.numero,
       MAX(p.estado_produccion) AS estado,
       MAX(p.cantidad) AS plan_kg,
       MAX(ISNULL(p.cantidad_fabricada, 0)) AS fab_padre,
       MAX(ISNULL(h.fab, 0)) AS fab_hijas
  FROM p
  LEFT JOIN h ON h.padre = p.id_orden_fabricacion
 GROUP BY p.numero
"""
    rows, ok = metabase_client.fetch_dataset_estado(ASINFO_DB, sql, max_results=5000)
    return {str(r.get("numero") or "").strip().upper(): r for r in rows or []}, ok


def _clientes_asinfo(pedidos: list[str]) -> tuple[dict, bool]:
    if not pedidos:
        return {}, True
    from modules._lib import metabase_client
    in_list = ", ".join(f"'{p}'" for p in pedidos)
    sql = f"""
SELECT p.numero, ISNULL(e.nombre_comercial, '') AS codigo,
       ISNULL(e.nombre_fiscal, '') AS nombre
  FROM pedido_cliente p
  LEFT JOIN empresa e ON e.id_empresa = p.id_empresa
 WHERE p.numero IN ({in_list})
"""
    rows, ok = metabase_client.fetch_dataset_estado(ASINFO_DB, sql, max_results=5000)
    return {str(r.get("numero") or "").strip().upper(): r for r in rows or []}, ok


def _dias(fecha: str, hoy: date | None) -> int | None:
    """'dd/mm/aaaa' de formulas → días hasta hoy."""
    try:
        d, m, a = (int(x) for x in str(fecha).strip()[:10].split("/"))
        return ((hoy or date.today()) - date(a, m, d)).days
    except (ValueError, TypeError):
        return None


def armar(filas: list[dict], ofts: dict, clientes: dict,
          hoy: date | None = None) -> list[dict]:
    """Junta las tres fuentes en una fila por orden de tintura."""
    out = []
    for r in filas:
        oft = _seguro(str(r.get("oft_numero") or ""))
        ped = _seguro(str(r.get("pedido_numero") or ""))
        a = ofts.get(oft)
        plan = _f((a or {}).get("plan_kg")) or _f(r.get("plan_kg"))
        fab = None
        estado = None
        if a is not None:
            fab = _f(a.get("fab_padre")) or _f(a.get("fab_hijas"))
            cerrada = int(_f(a.get("estado"))) == ESTADO_FINALIZADA
            if fab <= 0:
                estado = "en_tintura"
            elif not cerrada and plan and fab / plan < PARCIAL_HASTA:
                estado = "parcial"
            else:
                estado = "en_bodega"
        dias = _dias(r.get("fecha"), hoy)
        c = clientes.get(ped) or {}
        out.append({
            "orden": str(r.get("numero") or "").strip(),
            "fecha": str(r.get("fecha") or "").strip(),
            "oft": oft,
            "pedido": ped,
            "cliente_cod": str(c.get("codigo") or "").strip(),
            "cliente": str(c.get("nombre") or "").strip(),
            "producto": str(r.get("producto") or r.get("codigo") or "").strip(),
            "producto_cod": str(r.get("producto_codigo") or "").strip(),
            "fab": fab,
            "plan": plan,
            "pct": (min(round(fab / plan * 100), 100) if (fab is not None and plan) else None),
            "estado": estado,
            "dias": dias,
            "demora": bool(estado == "en_tintura" and dias is not None and dias > DIAS_DEMORA),
        })
    return out


def ordenes(dias: int = DIAS_DEFAULT, hoy: date | None = None) -> dict:
    """{"ok": formulas contestó, "asinfo": Asinfo contestó, "filas": [...]}"""
    if hoy is None:
        from filters import today_ec
        hoy = today_ec()
    dias = max(1, min(int(dias), 120))
    clave = (dias, hoy)
    with _LOCK:
        hit = _CACHE.get(clave)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        filas = _ordenes_formulas(hoy - timedelta(days=dias))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("ordenes: formulas no contestó: %s", e)
        return {"ok": False, "asinfo": False, "filas": []}
    ofts = sorted({_seguro(str(r.get("oft_numero") or "")) for r in filas} - {""})
    peds = sorted({_seguro(str(r.get("pedido_numero") or "")) for r in filas} - {""})
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(_ofts_asinfo, ofts)
            fc = ex.submit(_clientes_asinfo, peds)
            a, ok_a = fa.result()
            c, ok_c = fc.result()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("ordenes: Asinfo no contestó: %s", e)
        a, c, ok_a, ok_c = {}, {}, False, False
    res = {"ok": True, "asinfo": bool(ok_a and ok_c), "filas": armar(filas, a, c, hoy)}
    if res["asinfo"]:
        with _LOCK:
            _CACHE[clave] = (time.time(), res)
    return res


def contar(filas: list[dict]) -> dict:
    n = {k: 0 for k, _ in ESTADOS}
    for f in filas:
        if f["estado"] in n:
            n[f["estado"]] += 1
    n["todas"] = len(filas)
    return n
