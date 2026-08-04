"""Endpoint /admin/debug-terminado-otros — de dónde salen los "otros mov." de
la bodega de TERMINADO (53) en Asinfo. SOLO LECTURA.

TMT 2026-08-04 (dueña, mirando /produccion-terminado-asinfo: *"sí, esto me
suena raro"* · *"dale, hacé un deep dive y me contás qué son y decidimos"*).

La pantalla ya muestra QUÉ tan grande es el descuadre —34,5 t en 2026 que no
entraron por producción ni salieron por venta— pero no QUÉ es cada movimiento.
Este endpoint abre la caja: lista los deltas individuales del saldo por lote de
la bodega 53 y busca en el esquema de Asinfo qué documento los puede explicar.

Es un instrumento de investigación, no una pantalla: devuelve JSON, está detrás
de `admin_dbase.ver` y no escribe nada en ningún lado. Si la investigación
termina en algo que la dueña va a querer mirar seguido, eso se convierte en una
pantalla de verdad (ver skill `operar-por-la-ui`).

Modos (todos GET, todo sanitizado antes de interpolar):

    ?tablas=devol          — tablas de Asinfo cuyo nombre contiene el texto.
    ?meta=<tabla>          — columnas de esa tabla (INFORMATION_SCHEMA).
    ?deltas=YYYY-MM        — los movimientos de saldo de bodega 53 del mes,
                             de mayor a menor, con producto, lote, descripción
                             y usuario. &signo=ingreso|egreso · &n=200
    ?perfil=YYYY-MM        — cómo se reparten esos movimientos por TAMAÑO.
                             ¿Pocos grandes (algo que ir a buscar) o miles
                             chicos (ruido)? Un promedio no lo contesta.
    (sin parámetros)       — resumen por mes de 2026: ingreso/egreso real de la
                             bodega contra producción declarada y despacho.
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_terminado_otros",
    __name__,
    url_prefix="/admin/debug-terminado-otros",
)

BODEGA_TERMINADO = 53

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MES_RE = re.compile(r"^\d{4}-\d{2}$")


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _rango(mes: str) -> tuple[str, str]:
    """('YYYY-MM') → (primero del mes, primero del siguiente)."""
    y, m = (int(x) for x in mes.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        # 200 a propósito: el smoke de rutas del CI corre sin Metabase y un 5xx
        # en un GET estático rompe la suite. Fail-soft, igual que sus hermanos.
        return _json({"ok": False, "error": "Metabase no configurado"})

    # --- Modo tablas: buscar por nombre --------------------------------------
    tablas = (request.args.get("tablas") or "").strip()
    if tablas:
        if not _IDENT_RE.match(tablas):
            return _json({"ok": False, "error": "texto inválido"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT TABLE_NAME
              FROM INFORMATION_SCHEMA.TABLES
             WHERE TABLE_NAME LIKE '%{tablas}%'
             ORDER BY TABLE_NAME
        """, max_results=300)
        return _json({"ok": True, "modo": "tablas", "buscando": tablas,
                      "n": len(rows or []), "rows": rows})

    # --- Modo meta: columnas de una tabla ------------------------------------
    meta = (request.args.get("meta") or "").strip()
    if meta:
        if not _IDENT_RE.match(meta):
            return _json({"ok": False, "error": "tabla inválida"}, 400)
        rows = mc.fetch_dataset(2, f"""
            SELECT COLUMN_NAME, DATA_TYPE
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME = '{meta}'
             ORDER BY ORDINAL_POSITION
        """, max_results=300)
        return _json({"ok": True, "modo": "meta", "tabla": meta,
                      "n": len(rows or []), "rows": rows})

    # --- Modo deltas: los movimientos individuales del mes -------------------
    mes = (request.args.get("deltas") or "").strip()
    if mes:
        if not _MES_RE.match(mes):
            return _json({"ok": False, "error": "mes inválido (YYYY-MM)"}, 400)
        d1, d2 = _rango(mes)
        signo = (request.args.get("signo") or "ingreso").strip().lower()
        if signo not in ("ingreso", "egreso"):
            return _json({"ok": False, "error": "signo inválido"}, 400)
        try:
            n = max(1, min(int(request.args.get("n") or 200), 1000))
        except (TypeError, ValueError):
            n = 200
        # El delta > 0 es ingreso; < 0 egreso. `prev IS NULL` = alta de lote.
        # ⚠ Sin joins a `producto`/`lote`: la primera versión los tenía y la
        # query devolvía [] (fail-soft se come el error). `saldo_producto_lote`
        # trae `descripcion` y `usuario_creacion` propios, que es justo lo que
        # sirve para saber QUÉ es cada movimiento.
        cond = ("(prev IS NULL AND saldo > 0) OR (saldo - prev > 0)"
                if signo == "ingreso" else "saldo - prev < 0")
        orden = "delta DESC" if signo == "ingreso" else "delta ASC"
        rows = mc.fetch_dataset(2, f"""
            WITH d AS (
                SELECT id_producto, id_lote, fecha, saldo, descripcion,
                       usuario_creacion,
                       LAG(saldo) OVER (
                           PARTITION BY id_producto, id_lote
                           ORDER BY fecha, id_saldo_producto_lote
                       ) AS prev
                  FROM saldo_producto_lote
                 WHERE id_bodega = {BODEGA_TERMINADO}
            )
            SELECT TOP {n}
                   CONVERT(varchar(10), fecha, 23) AS dia,
                   id_producto, id_lote, prev, saldo, descripcion,
                   usuario_creacion,
                   CASE WHEN prev IS NULL THEN saldo
                        ELSE saldo - prev END AS delta
              FROM d
             WHERE fecha >= '{d1}' AND fecha < '{d2}'
               AND ({cond})
             ORDER BY {orden}
        """, max_results=n)
        total = sum(float(r.get("delta") or 0) for r in (rows or []))
        return _json({"ok": True, "modo": "deltas", "mes": mes, "signo": signo,
                      "n": len(rows or []), "suma_kg": round(total, 2),
                      "rows": rows})

    # --- Modo perfil: cómo se reparten los movimientos por tamaño ------------
    # La pregunta que decide la historia: ¿son POCOS movimientos GRANDES
    # (devoluciones, ajustes — algo que ir a buscar) o MILES chicos (ruido de
    # cómo Asinfo escribe el saldo)? Un promedio no lo contesta; la
    # distribución sí.
    perfil = (request.args.get("perfil") or "").strip()
    if perfil:
        if not _MES_RE.match(perfil):
            return _json({"ok": False, "error": "mes inválido (YYYY-MM)"}, 400)
        d1, d2 = _rango(perfil)
        rows = mc.fetch_dataset(2, f"""
            WITH d AS (
                SELECT fecha, saldo,
                       LAG(saldo) OVER (
                           PARTITION BY id_producto, id_lote
                           ORDER BY fecha, id_saldo_producto_lote
                       ) AS prev
                  FROM saldo_producto_lote
                 WHERE id_bodega = {BODEGA_TERMINADO}
            ),
            m AS (
                SELECT CASE WHEN prev IS NULL THEN saldo ELSE saldo - prev END AS delta,
                       CASE WHEN prev IS NULL THEN 1 ELSE 0 END AS es_alta
                  FROM d
                 WHERE fecha >= '{d1}' AND fecha < '{d2}'
                   AND (prev IS NULL OR saldo <> prev)
            )
            SELECT CASE WHEN ABS(delta) >= 1000 THEN 'e 1000+'
                        WHEN ABS(delta) >=  500 THEN 'd 500-1000'
                        WHEN ABS(delta) >=  100 THEN 'c 100-500'
                        WHEN ABS(delta) >=   10 THEN 'b 10-100'
                        ELSE 'a 0-10' END AS tramo,
                   CASE WHEN delta > 0 THEN 'ingreso' ELSE 'egreso' END AS signo,
                   COUNT(*)   AS n,
                   SUM(delta) AS kg,
                   SUM(es_alta) AS altas_lote
              FROM m
             WHERE delta <> 0
             GROUP BY CASE WHEN ABS(delta) >= 1000 THEN 'e 1000+'
                           WHEN ABS(delta) >=  500 THEN 'd 500-1000'
                           WHEN ABS(delta) >=  100 THEN 'c 100-500'
                           WHEN ABS(delta) >=   10 THEN 'b 10-100'
                           ELSE 'a 0-10' END,
                      CASE WHEN delta > 0 THEN 'ingreso' ELSE 'egreso' END
             ORDER BY signo, tramo
        """, max_results=50)
        return _json({"ok": True, "modo": "perfil", "mes": perfil,
                      "n": len(rows or []), "rows": rows})

    # --- Default: el resumen del año ----------------------------------------
    anio = (request.args.get("anio") or "2026").strip()
    if not re.fullmatch(r"\d{4}", anio):
        return _json({"ok": False, "error": "año inválido"}, 400)
    d1, d2 = f"{anio}-01-01", f"{int(anio) + 1}-01-01"
    rows = mc.fetch_dataset(2, f"""
        WITH d AS (
            SELECT saldo, fecha,
                   LAG(saldo) OVER (
                       PARTITION BY id_producto, id_lote
                       ORDER BY fecha, id_saldo_producto_lote
                   ) AS prev
              FROM saldo_producto_lote
             WHERE id_bodega = {BODEGA_TERMINADO}
        )
        SELECT CONVERT(varchar(7), fecha, 23) AS mes,
               SUM(CASE WHEN prev IS NULL AND saldo > 0 THEN saldo
                        WHEN prev IS NOT NULL AND saldo - prev > 0 THEN saldo - prev
                        ELSE 0 END) AS ingreso,
               SUM(CASE WHEN prev IS NOT NULL AND saldo - prev < 0 THEN prev - saldo
                        ELSE 0 END) AS egreso,
               SUM(CASE WHEN prev IS NULL AND saldo > 0 THEN 1 ELSE 0 END) AS altas_lote
          FROM d
         WHERE fecha >= '{d1}' AND fecha < '{d2}'
         GROUP BY CONVERT(varchar(7), fecha, 23)
         ORDER BY mes
    """, max_results=50)
    return _json({"ok": True, "modo": "resumen", "anio": anio,
                  "bodega": BODEGA_TERMINADO, "rows": rows})
