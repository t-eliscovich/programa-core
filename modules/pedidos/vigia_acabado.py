"""Vigía del ACABADO de los pedidos (06/10/2026) — health `acabado_pedidos`.

Tamara, tras el PDCL-32677 (GIC, FE96MAR: ABI en Asinfo, TUB en /pedidos):
*"no nos puede seguir pasando"*. Ya había pasado con el memo (PDCL-31867,
21/09) y antes con el corte Pedido (PDCL-31577, 10/09). Cada vez se arregló
UNA pantalla y la otra seguía mintiendo sin que nadie lo viera.

Este vigía compara TODAS las pantallas que muestran el acabado de un pedido
contra Asinfo:

  * /pedidos — cortes Color y Tipo de tela (`service.pendientes`): cada
    (producto, acabado) pedido tiene que tener SU renglón, con los mismos
    pedidos; y no puede haber un renglón con un acabado que nadie pidió.
  * /pedidos corte Pedido, /mi-cartera (pestaña Pedidos) y la foto que arma
    el memo al mandarse (`service.por_pedido`): cada línea con el acabado de
    su línea en Asinfo.
  * Los memos VIVOS ya mandados a la fábrica (formulas_app): el acabado de
    cada línea de la foto contra Asinfo.
  * Las ventas de Saldos (Vendidos y el detalle de la Competencia, tabla
    `parado_venta`) de los últimos `DIAS_VENTAS` días: el acabado guardado
    contra el de la línea de factura (07/10/2026).

⭐ La verdad se lee por OTRO camino que el de las pantallas: acá el atributo
se encuentra por `valor_atributo.id_atributo = 1` entre los diez valores de
la línea, y las pantallas lo buscan por `id_atributo_N = 1`
(`service._SQL_VALOR_ACABADO_LINEA`). Si un día alguien rompe la regla
única, los dos caminos dejan de coincidir y esto suena.

Si Asinfo no contesta: `ok` con `sin_datos` (no poder mirar no es estar mal;
mismo criterio que hilo_local).
"""
from __future__ import annotations

import logging

from modules._lib import metabase_client

from . import service

_LOG = logging.getLogger("programa_core.pedidos.vigia_acabado")

_SQL_VERDAD = """
SELECT v.numero, pr.codigo, pr.nombre_categoria_producto AS categoria,
       va.codigo AS acabado
  FROM v_saldos_comprometidos_detallado v
  JOIN producto pr ON pr.id_producto = v.id_producto
  JOIN detalle_pedido_cliente d
    ON d.id_pedido_cliente = v.id_pedido_cliente
   AND d.id_producto = v.id_producto
  JOIN valor_atributo va
    ON va.id_atributo = 1
   AND va.id_valor_atributo IN (
         d.id_valor_atributo_1, d.id_valor_atributo_2, d.id_valor_atributo_3,
         d.id_valor_atributo_4, d.id_valor_atributo_5, d.id_valor_atributo_6,
         d.id_valor_atributo_7, d.id_valor_atributo_8, d.id_valor_atributo_9,
         d.id_valor_atributo_10)
 WHERE DATEDIFF(day, v.fecha, {ahora}) <= {dias}
 GROUP BY v.numero, pr.codigo, pr.nombre_categoria_producto, va.codigo
"""

#: Cuántos casos se nombran en cada aviso (el resto va en el número).
_MUESTRA = 5

#: Cuántos días de ventas de Saldos se comparan (la consulta entera desde la
#: largada tarda ~30 s; dos semanas alcanzan para ver si el refresco anda).
DIAS_VENTAS = 14

#: El acabado de cada venta, por OTRO camino que el refresco de Saldos: el
#: valor con `id_atributo = 1` entre los de la línea; si la línea no trae
#: (nota de crédito), el de la madre.
_SQL_VENTAS = """
SELECT RTRIM(fc.numero) AS numero,
       pr.nombre_subcategoria_producto AS subcategoria,
       RIGHT(RTRIM(pr.codigo), 3) AS color,
       COALESCE(
         (SELECT MAX(va.codigo) FROM valor_atributo va
           WHERE va.id_atributo = 1 AND va.id_valor_atributo IN (
             dfc.id_valor_atributo_1, dfc.id_valor_atributo_2, dfc.id_valor_atributo_3,
             dfc.id_valor_atributo_4, dfc.id_valor_atributo_5, dfc.id_valor_atributo_6,
             dfc.id_valor_atributo_7, dfc.id_valor_atributo_8, dfc.id_valor_atributo_9,
             dfc.id_valor_atributo_10)),
         (SELECT MAX(va.codigo) FROM detalle_factura_cliente m
            JOIN valor_atributo va ON va.id_atributo = 1 AND va.id_valor_atributo IN (
             m.id_valor_atributo_1, m.id_valor_atributo_2, m.id_valor_atributo_3,
             m.id_valor_atributo_4, m.id_valor_atributo_5, m.id_valor_atributo_6,
             m.id_valor_atributo_7, m.id_valor_atributo_8, m.id_valor_atributo_9,
             m.id_valor_atributo_10)
           WHERE m.id_factura_cliente = fc.id_factura_cliente_padre
             AND m.id_producto = dfc.id_producto)) AS acabado
  FROM factura_cliente fc
  JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
  JOIN producto pr ON pr.id_producto = dfc.id_producto
 WHERE fc.id_documento IN (7, 251, 20, 451) AND fc.estado NOT IN (0, 1)
   AND dfc.cantidad > 0
   AND fc.fecha >= DATEADD(day, -{dias}, CAST({ahora} AS date))
"""


def verdad() -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], bool]:
    """`({(numero, producto): acabado}, {(numero, producto): categoría}, ok)`
    de lo pendiente, leído de Asinfo. El acabado es 'ABI', 'TUB' o 'ABI/TUB'
    (el mismo producto pedido en los dos)."""
    filas, ok = metabase_client.fetch_dataset_estado(
        service.ASINFO_DB, _SQL_VERDAD.format(
            ahora=service.AHORA_EC, dias=service.DIAS_PEDIDO_MAX))
    if not ok:
        return {}, {}, False
    sets: dict[tuple[str, str], set[str]] = {}
    cats: dict[tuple[str, str], str] = {}
    for r in filas:
        numero = str(r.get("numero") or "").strip().upper()
        cod = str(r.get("codigo") or "").strip()
        aca = str(r.get("acabado") or "").strip().upper()
        if not (numero and cod and aca):
            continue
        sets.setdefault((numero, cod), set()).add(aca)
        cats[(numero, cod)] = str(r.get("categoria") or "").strip()
    return {k: "/".join(sorted(v)) for k, v in sets.items()}, cats, True


def comparar_renglones(lineas: dict[tuple[str, str], str],
                       categorias: dict[tuple[str, str], str],
                       filas: list[dict]) -> list[str]:
    """Los renglones de los cortes Color / Tipo de tela contra Asinfo.
    Devuelve los problemas en castellano (vacío = coincide)."""
    esperado: dict[tuple[str, str], set[str]] = {}
    for (numero, cod), aca in lineas.items():
        if categorias.get((numero, cod)) in service.CATEGORIAS:
            esperado.setdefault((cod, aca), set()).add(numero)
    en_pantalla = {(f["codigo"], f.get("acabado") or ""): f for f in filas}
    problemas = []
    for (cod, aca), numeros in sorted(esperado.items()):
        f = en_pantalla.get((cod, aca))
        if f is None:
            otros = sorted(a or "sin acabado" for c, a in en_pantalla if c == cod)
            problemas.append(
                f"{cod} pedido en {aca} ({', '.join(sorted(numeros)[:2])}) "
                f"no tiene renglón {aca}"
                + (f" — sale como {', '.join(otros)}" if otros else ""))
        elif int(f.get("n_pedidos") or 0) != len(numeros):
            problemas.append(
                f"{cod} {aca}: la pantalla cuenta {f.get('n_pedidos')} pedidos "
                f"y Asinfo {len(numeros)}")
    for (cod, aca) in sorted(en_pantalla):
        if aca and (cod, aca) not in esperado:
            problemas.append(f"{cod} sale como {aca} y nadie lo pidió así")
    return problemas


def comparar_lineas(lineas: dict[tuple[str, str], str],
                    pedidos: list[dict]) -> list[str]:
    """Las líneas del corte Pedido / /mi-cartera / el memo contra Asinfo."""
    problemas = []
    for p in pedidos:
        numero = str(p.get("numero") or "").strip().upper()
        for ln in p.get("lineas") or []:
            verdad_ = lineas.get((numero, ln.get("producto")))
            if verdad_ is not None and (ln.get("acabado") or "") != verdad_:
                problemas.append(
                    f"{numero} {ln.get('producto')}: dice "
                    f"{ln.get('acabado') or 'sin acabado'} y Asinfo {verdad_}")
    return problemas


def comparar_memos(lineas: dict[tuple[str, str], str],
                   memos: list[dict]) -> list[str]:
    """Los memos vivos ya mandados a la fábrica contra Asinfo."""
    problemas = []
    for m in memos:
        numero = str(m.get("numero") or "").strip().upper()
        for ln in (m.get("detalle") or {}).get("lineas") or []:
            verdad_ = lineas.get((numero, ln.get("producto")))
            if verdad_ is not None and (ln.get("acabado") or "") != verdad_:
                problemas.append(
                    f"memo {numero} {ln.get('producto')}: dice "
                    f"{ln.get('acabado') or 'sin acabado'} y Asinfo {verdad_}")
    return problemas


def ventas_saldos() -> tuple[list[str], int, bool]:
    """Las ventas de Saldos guardadas (`parado_venta`) contra Asinfo.
    `(problemas, comparadas, ok)`; ok=False si Asinfo no contestó."""
    filas, ok = metabase_client.fetch_dataset_estado(
        service.ASINFO_DB, _SQL_VENTAS.format(
            dias=DIAS_VENTAS, ahora=service.AHORA_EC), max_results=50000)
    if not ok:
        return [], 0, False
    verdad: dict[tuple[str, str, str], set[str]] = {}
    for r in filas:
        aca = str(r.get("acabado") or "").strip().upper()
        if aca:
            verdad.setdefault((str(r.get("numero") or "").strip(),
                               str(r.get("subcategoria") or "").strip(),
                               str(r.get("color") or "").strip()), set()).add(aca)
    import db
    guardadas = db.fetch_all(
        "SELECT numero, subcategoria, color, acabado FROM scintela.parado_venta "
        "WHERE acabado IS NOT NULL AND numero IS NOT NULL "
        "AND fecha >= CURRENT_DATE - %s", (DIAS_VENTAS,)) or []
    problemas = []
    for g in guardadas:
        k = (str(g["numero"]).strip(), str(g["subcategoria"]).strip(),
             str(g["color"]).strip())
        v = verdad.get(k)
        if v is not None and g["acabado"] not in v:
            problemas.append(f"{k[0]} {k[1]} {k[2]}: dice {g['acabado']} y "
                             f"Asinfo {'/'.join(sorted(v))}")
    return problemas, len(guardadas), True


def _aviso(categoria: str, donde: str, problemas: list[str]) -> dict:
    muestra = "; ".join(problemas[:_MUESTRA])
    resto = len(problemas) - _MUESTRA
    return {
        "severity": "high", "category": categoria,
        "msg": (f"{donde}: {len(problemas)} acabado"
                f"{'' if len(problemas) == 1 else 's'} distinto"
                f"{'' if len(problemas) == 1 else 's'} de Asinfo ({muestra}"
                f"{f' y {resto} más' if resto > 0 else ''})."),
    }


def health() -> dict:
    """{ok, alerts, stats} para /admin/health/all. Nunca levanta."""
    try:
        lineas, categorias, ok = verdad()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("vigía acabado: %s", e)
        return {"ok": True, "alerts": [], "stats": {"sin_datos": str(e)[:120]}}
    if not ok:
        return {"ok": True, "alerts": [], "stats": {"sin_datos": "Asinfo no contestó"}}

    alerts: list[dict] = []
    stats: dict = {"lineas": len(lineas),
                   "abi": sum(1 for a in lineas.values() if a == "ABI"),
                   "tub": sum(1 for a in lineas.values() if a == "TUB")}

    filas, ok_f = service.pendientes(fresco=True)
    if ok_f:
        p = comparar_renglones(lineas, categorias, filas)
        stats["renglones"] = len(filas)
        if p:
            alerts.append(_aviso("acabado_pedidos_renglon",
                                 "/pedidos (Color y Tipo de tela)", p))

    pedidos, ok_p = service.por_pedido(fresco=True)
    if ok_p:
        p = comparar_lineas(lineas, pedidos)
        if p:
            alerts.append(_aviso("acabado_pedidos_linea",
                                 "/pedidos corte Pedido, /mi-cartera y el memo", p))

    from modules._lib import formulas_memos
    memos = formulas_memos.vivos()
    stats["memos_vivos"] = len(memos)
    p = comparar_memos(lineas, memos)
    if p:
        alerts.append(_aviso("acabado_pedidos_memo",
                             "Memos ya mandados a la fábrica", p))

    try:
        p, n, ok_v = ventas_saldos()
        stats["ventas_saldos"] = n if ok_v else "sin_datos"
        if p:
            alerts.append(_aviso("acabado_saldos_venta",
                                 "Saldos (Vendidos y Competencia)", p))
    except Exception as e:  # noqa: BLE001 — sin esta parte, el resto vale
        _LOG.warning("vigía acabado (saldos): %s", e)
        stats["ventas_saldos"] = "error"

    mixtos = sorted(f"{n} {c}" for (n, c), a in lineas.items() if "/" in a)
    stats["mixtos"] = len(mixtos)
    if mixtos:
        alerts.append({
            "severity": "medium", "category": "acabado_pedidos_mixto",
            "msg": (f"{len(mixtos)} pedido{'' if len(mixtos) == 1 else 's'} "
                    f"piden el mismo producto en ABI y en TUB "
                    f"({', '.join(mixtos[:_MUESTRA])}): Asinfo no dice qué "
                    f"saldo es de cuál, así que el renglón sale ABI/TUB."),
        })
    return {"ok": not any(a["severity"] == "high" for a in alerts),
            "alerts": alerts, "stats": stats}
