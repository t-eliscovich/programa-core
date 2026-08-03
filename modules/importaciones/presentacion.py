"""Cómo se MUESTRAN las importaciones en /importaciones. Sólo pantalla.

⚠️ NADA de este módulo puede entrar al camino del balance, de la tarifa del
hilado, de `autobap` ni de la alarma de banda. Ésos leen
`service.importaciones_con_cruce()` **fila por fila**, y ahí una importación
partida son DOS documentos que suman sus kilos por separado. Si el colapso se
colara en ese camino, cada mitad pasaría a llevar el kg del grupo entero y el
costo del hilo se iría al doble. La regla es de una línea:

    el dato queda como está; lo único que se junta es lo que se dibuja.

TMT 2026-08-03 (dueña, sobre AI 15): *"sabemos que hay dos AI 15 que partimos
los kg en dos 11k y 11. deberían ir todas en una, ¿cuál sería una manera
elegante de agrupar y que ambas queden pagadas?"*.

La respuesta a "que ambas queden pagadas" es que **no son dos**: IM-0000571 e
IM-0000572 son la misma factura del proveedor (`AYF02653`), 11.289,39 kg cada
una, recibidas el mismo día. Colapsadas en una fila, el grupo tiene sus
22.578,78 kg, sus compras una sola vez y **una sola** marca de pagada.
"""
from __future__ import annotations


def _fecha_max(vals) -> str | None:
    xs = sorted(str(v)[:10] for v in vals if v)
    return xs[-1] if xs else None


def _fecha_min(vals) -> str | None:
    xs = sorted(str(v)[:10] for v in vals if v)
    return xs[0] if xs else None


def _colapsable(miembros: list[dict]) -> bool:
    """Sólo se junta lo que es sin dudas la misma mercadería y está en el mismo
    momento del circuito.

    Se exige que **todas** las mitades estén en el mismo estado de recepción. Un
    grupo a medio llegar se deja abierto a propósito: colapsarlo obligaría a
    inventar una respuesta para "¿está recibida?" — y ninguna de las dos sería
    cierta. Mejor dos filas honestas que una fila que miente.
    """
    if len(miembros) < 2:
        return False
    if any(m.get("origen") != "importacion" for m in miembros):
        return False
    if not all(m.get("grupo_completo") for m in miembros):
        return False
    recibidas = {bool(m.get("recibida")) for m in miembros}
    return len(recibidas) == 1


def _unir_compras(miembros: list[dict]) -> dict | None:
    """Las compras del grupo, **deduplicadas por `id_compra`**.

    Hoy el cruce le asigna cada compra a UNA sola mitad (`_nearest_import`), así
    que en la práctica no hay repetidos — pero el dedup va igual: el día que la
    atribución cambie, sin él la pantalla mostraría el doble de plata y nadie
    lo notaría hasta que alguien sumara a mano.
    """
    items: dict = {}
    ids: list = []
    tipo = ""
    for m in miembros:
        c = m.get("compra") or {}
        for it in (c.get("items") or []):
            k = it.get("id_compra")
            if k in items:
                continue
            items[k] = it
            ids.append(k)
        tipo = tipo or (c.get("tipo") or "")
    if not items:
        return None
    orden = sorted(items.values(), key=lambda x: str(x.get("fecha") or ""),
                   reverse=True)
    return {
        "ids": ids,
        "first_id": orden[0].get("id_compra") if orden else None,
        "importe_total": round(sum(float(i.get("importe") or 0) for i in orden), 2),
        "n": len(orden),
        "tipo": tipo,
        "items": orden,
    }


def _unir_anticipos(miembros: list[dict]) -> dict | None:
    """Los anticipos del grupo.

    No se deduplica: cada partida de `scintela.dolares` se atribuye a UNA sola
    importación (`ant_por_im`), así que concatenar es exacto. Deduplicar por
    (fecha, importe) sería peor — dos anticipos iguales el mismo día existen.
    """
    items: list = []
    for m in miembros:
        items.extend(((m.get("anticipo") or {}).get("items") or []))
    if not items:
        return None
    items.sort(key=lambda x: str(x.get("fecha") or ""), reverse=True)
    return {
        "importe_total": round(sum(float(i.get("importe") or 0) for i in items), 2),
        "n": len(items),
        "items": items,
    }


def _unir_movimientos(miembros: list[dict]) -> list:
    """Los pagos del grupo, deduplicados por `id_transaccion` (una ND puede
    estar colgada de las dos mitades)."""
    vistos: set = set()
    out: list = []
    for m in miembros:
        for mv in (m.get("movimientos") or []):
            k = mv.get("id_transaccion") or (mv.get("fecha"), mv.get("monto"),
                                             mv.get("tipo"))
            if k in vistos:
                continue
            vistos.add(k)
            out.append(mv)
    out.sort(key=lambda x: str(x.get("fecha") or ""), reverse=True)
    return out


def colapsar_partidas(rows: list[dict], estado_pago=None) -> list[dict]:
    """Junta las mitades de una importación partida en UNA fila de pantalla.

    `estado_pago`: callable que recibe los items de compra y devuelve
    {"pagada", "saldo", "parcial"}. Se **recalcula** sobre las compras unidas —
    tomar el estado de una mitad daría "sin cargar" en la que no quedó con la
    plata, que es justo lo que se ve hoy en AI 15.

    Devuelve una lista nueva; **no muta** las filas de entrada (son las mismas
    que devuelve el cache de Asinfo y las comparte el resto del programa).
    """
    grupos: dict = {}
    for r in rows or []:
        gid = str(r.get("grupo_id") or "")
        if r.get("origen") == "importacion" and len(r.get("grupo_ims") or []) > 1:
            grupos.setdefault(gid, []).append(r)

    a_colapsar = {gid: ms for gid, ms in grupos.items() if _colapsable(ms)}
    if not a_colapsar:
        return list(rows or [])

    hechos: set = set()
    out: list = []
    for r in rows or []:
        gid = str(r.get("grupo_id") or "")
        miembros = a_colapsar.get(gid)
        if not miembros:
            out.append(r)
            continue
        if gid in hechos:
            continue        # la otra mitad ya salió dentro de la fila del grupo
        hechos.add(gid)

        base = dict(miembros[0])
        ims = sorted(str(m.get("im_numero") or "") for m in miembros)
        base["im_numero"] = " + ".join(ims)
        base["partidas_ims"] = ims
        base["es_grupo"] = True
        base["n_partidas"] = len(miembros)
        # Kg del GRUPO. `grupo_kg` ya lo trae calculado el service; se usa ése y
        # no una suma nueva, para que pantalla y balance lean el mismo número.
        base["kg"] = miembros[0].get("grupo_kg")
        _kgr = [float(m.get("kg_recibidos") or 0) for m in miembros]
        base["kg_recibidos"] = round(sum(_kgr), 2) if any(_kgr) else None
        # La importación está completa cuando llegó la ÚLTIMA mitad.
        base["fecha"] = _fecha_min(m.get("fecha") for m in miembros)
        base["fecha_recepcion"] = _fecha_max(
            m.get("fecha_recepcion") for m in miembros)
        base["fecha_recepcion_pc"] = _fecha_max(
            m.get("fecha_recepcion_pc") for m in miembros)
        base["recibido_pc"] = any(m.get("recibido_pc") for m in miembros)

        # La nota del grupo es la del PROVEEDOR, sin el ordinal de la partida:
        # en una fila que ya dice "2 partidas", un "----2" suelto confunde más
        # de lo que informa (parecía que se muestra sólo la mitad 2).
        try:
            from .service import _nota_base as _nb
            _base_nota = _nb(miembros[0].get("nota"))
            _cod = str(miembros[0].get("codigo") or "").strip()
            base["nota"] = f"{_base_nota} ( {_cod} )" if _cod else _base_nota
        except Exception:  # noqa: BLE001 -- si falla, queda la nota cruda
            pass

        base["compra"] = _unir_compras(miembros)
        base["anticipo"] = _unir_anticipos(miembros)
        base["movimientos"] = _unir_movimientos(miembros)
        base["anticipo_aplicado"] = round(
            sum(float(m.get("anticipo_aplicado") or 0) for m in miembros), 2)
        base["fuente"] = ("compra" if base["compra"]
                          else ("anticipo" if base["anticipo"] else None))
        base["importe_programa"] = (
            (base["compra"] or {}).get("importe_total")
            if base["compra"] else (base["anticipo"] or {}).get("importe_total")
        )
        # Estado de pago RECALCULADO sobre las compras unidas.
        est = None
        if estado_pago and base["compra"]:
            try:
                est = estado_pago(base["compra"]["items"])
            except Exception:  # noqa: BLE001 -- la pantalla nunca se cae por esto
                est = None
        base["pagada"] = bool(est and est.get("pagada"))
        base["saldo"] = float(est["saldo"]) if est else 0.0
        base["parcial"] = bool(est and est.get("parcial"))
        out.append(base)
    return out
