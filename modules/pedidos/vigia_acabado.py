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
