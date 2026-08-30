"""Importaciones que llegaron y quedaron sin toda su plata cargada.

TMT 2026-07-31. La primera versión de esta alarma salía en rojo arriba del
Informe de Resultados y la dueña la sacó a los diez minutos: *"me sacás todo
esto en rojo de Resultados"*. Tenía razón, y el motivo importa más que el
cartel — su diagnóstico fue *"pero creo que lo cargará Andrés cuando llegue"*:

  · los KILOS entran a Asinfo el día que llega el contenedor;
  · la PLATA (factura, CAE, flete, seguro) se carga después, cuando llegan los
    papeles.

O sea que TODA importación pasa por una ventana en la que tiene todos los kilos
y sólo parte de la plata, y en esa ventana el US$/kg da bajo. La alarma no
encontraba un error: encontraba el estado normal de una importación reciente.

**Cuánto dura esa ventana, medido** (35 grupos con datos, 18 meses,
/admin/debug-maduracion-importacion): 13 ya tenían la plata al llegar
(anticipos); de los que tardaron, la mediana es 10 días, 9 de cada 10 cierran
en ≤ 19, 34 de 35 en ≤ 21, y el más lento fue AC 16 con 54.

De ahí sale el umbral: **30 días**. Por debajo, "está bajo" quiere decir "se
está cargando". Por encima, quiere decir "se quedó así".

Lo que encontró la medición al aplicarlo — y que nadie estaba viendo:

    AC 76-75   recibida 18/12/2025   225 días   0,17 US$/kg   50.200 kg
    AC 58      recibida 28/03/2026   125 días   0,35 US$/kg   23.430 kg
    MH 59      recibida 15/04/2026   107 días   1,84 US$/kg   24.150 kg
    MH 60      recibida 25/04/2026    97 días   1,78 US$/kg   24.300 kg
    MH 61      recibida 06/05/2026    86 días   1,92 US$/kg   24.300 kg
    MH 64-65   recibida 02/06/2026    59 días   2,34 US$/kg   47.730 kg

Seis importaciones de entre 2 y 7 meses con un solo movimiento cargado. Puede
ser plata sin cargar, o plata cargada que el cruce no le está atribuyendo
(la ventana de atribución es de 300 días). Cualquiera de las dos es un problema,
y ninguna se veía.

Va a la CAMPANITA, no al balance (dueña: *"nada de rojo en resultados, en la
campanita"*). Un aviso por grupo, con `clave` idempotente: se dice una vez y no
vuelve a repetirse en cada corrida.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time as _t
from datetime import date
from urllib.parse import quote_plus

_LOG = logging.getLogger("programa_core.importaciones.vigilancia")

# Umbral elegido por la dueña sobre la medición: 34 de 35 importaciones tienen
# toda su plata antes de los 21 días. A los 30 no queda maduración normal.
DIAS_SIN_PLATA_DEFAULT = 30

_FRENO_SECS = 6 * 3600          # una revisada cada 6 h alcanza y sobra
_ultima_corrida = 0.0
_lock = threading.Lock()

# Techo de antigüedad. **Esto lo aprendí rompiéndolo**: la primera versión no lo
# tenía y a los tres minutos de deployar había 200+ avisos en la campanita.
# Motivo: para una importación vieja el cruce no encuentra NINGUNA compra ni
# anticipo — los anticipos ya se convirtieron (dejan de estar vivos) y PC no
# tiene las compras de esa época — así que el $/kg da 0,00 y la alarma la lee
# como "no cargaron nada". No es "falta el CAE": es "PC nunca tuvo ese dato".
# Dos guardas, y las dos hacen falta:
#   · `_MAX_DIAS` — más viejo que esto no se toca;
#   · **al menos un movimiento cargado** — sin ni uno, no hay nada que comparar.
#
# TMT 2026-07-31, viendo los 8 que quedaron (de 37 a 225 días): *"borrá todo lo
# que ya pasó más de 31 días, no tiene sentido traer problemas de hace tanto
# tiempo"*. Con la alarma andando NO hay backlog: cada importación se agarra al
# cruzar el umbral y nunca llega a ser vieja. El techo de 31 es lo que hace que
# esto sea un aviso del día y no un inventario de pendientes históricos.
#
# El costo, dicho: la ventana de aviso es de un día (entre el 30 y el 31). El
# ciclo corre cada 6 h, así que hay ~4 oportunidades; si el servidor estuviera
# caído un día entero, esa importación no se avisaría nunca — y no vuelve.
# Las viejas no desaparecen del sistema, sólo dejan de avisar: se siguen viendo
# en /admin/importaciones-sin-plata?techo=0.
_MAX_DIAS_DEFAULT = 31


def _techo_dias() -> int:
    try:
        v = int(os.environ.get("IMPORT_SIN_PLATA_TECHO", _MAX_DIAS_DEFAULT))
        return v if v >= 1 else _MAX_DIAS_DEFAULT
    except (TypeError, ValueError):
        return _MAX_DIAS_DEFAULT


def _dias_umbral() -> int:
    try:
        v = int(os.environ.get("IMPORT_SIN_PLATA_DIAS", DIAS_SIN_PLATA_DEFAULT))
        return v if v >= 1 else DIAS_SIN_PLATA_DEFAULT
    except (TypeError, ValueError):
        return DIAS_SIN_PLATA_DEFAULT


# ── A dónde lleva el "ver →" ────────────────────────────────────────────────
# TMT 2026-08-26 (dueña, sobre el aviso de MH 66-67): *"acá el ver no me lleva a
# filtrado por esas importaciones"*. El aviso abría /importaciones entera y el
# caso quedaba perdido entre las demás.
#
# Dos cosas de esa pantalla hacen falta para que el link caiga bien:
#   · busca con `q`, y cada PALABRA tiene que aparecer en algún campo (proveedor,
#     nota, código, número de importación, factura). El código del programa
#     ("MH 66-67") es el MISMO string en las dos mitades de una partida, así que
#     buscarlo las trae juntas;
#   · por defecto muestra sólo el año en curso. Un aviso no puede depender de en
#     qué año cayó la recepción, así que va `anio=todos`.
_RE_CODIGO = re.compile(r"^[A-Z]{2,3} \d+(-\d+)?$")


def _url_filtrada(q: str) -> str:
    """La pantalla de importaciones ya filtrada, o la lista entera si no hay
    nada seguro que buscar: un link a de más miente menos que uno que no
    devuelve ninguna fila."""
    q = (q or "").strip()
    if not q:
        return "/importaciones"
    return "/importaciones?anio=todos&q=" + quote_plus(q)


def _q_del_caso(c: dict) -> str:
    """Qué buscar para que queden las filas de ESTE grupo.

    El código del programa si se pudo parsear; si la nota de Asinfo no lo tenía,
    `codigo` sale armado con None ("None None") y ahí se cae al número de la
    importación, que existe siempre.
    """
    cod = str(c.get("codigo") or "").strip()
    if _RE_CODIGO.match(cod):
        return cod
    ims = [str(i).strip() for i in (c.get("ims") or []) if i]
    return ims[0] if ims else ""


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


# ── Contra QUÉ se compara: el promedio de ESE tipo de hilado ────────────────
# TMT 2026-08-26. La alarma comparaba el US$/kg contra una banda fija de
# 2,7–3,4 y saltó con MH 66-67. Tamara se lo preguntó a Andrés y la respuesta
# terminó con la banda: *"No falta nada por pasar. Ese hilo es de poliéster,
# que tiene un precio menor al polialgodón. Un hilo de polialgodón está en este
# momento a 2,75, mientras que uno de poliéster está a 1,70"*.
#
# O sea que la banda no era una banda: era el precio de UN tipo de hilo. La
# alarma no encontraba un error, encontraba hilo más barato.
#
# Ahora cada grupo se compara con lo que vale SU hilo:
# `asinfo.importaciones_costo_estimado()` promedia el US$/kg por PRODUCTO con
# ventana de 3 meses (cae a 6 si ese hilado no se importó hace poco) y marca
# aparte los kilos sin histórico. Estaba escrito desde el 29/06 y sin usar.
#
# **Los cortes salen del dato**, no de la intuición — medidos el 26/08 sobre
# los 71 grupos con precio de los últimos 12 meses
# (/admin/debug-costo-importacion). El "esperado" es la mercadería; lo cargado
# en el programa incluye además CAE, flete y seguro, así que el ratio normal
# está ARRIBA de 1:
#
#     ratio = cargado ÷ esperado    mediana 1,06 · p90 1,26 · máximo 1,275
#     ninguno entre 0,19 y 0,90     ← el hueco donde cae el corte de abajo
#
#   · abajo de 0,85 no llega ni a lo que vale el hilo → falta plata. El más
#     bajo con algo cargado es AC 58 (0,18); el más bajo NORMAL, 0,902;
#   · arriba de 1,35 ya no es flete: son kilos que faltan (media importación
#     dobla el US$/kg). El máximo real medido fue 1,275.
RATIO_MINIMO = 0.85
RATIO_MAXIMO = 1.35


def _leer_costos(limite: int = 1000) -> dict | None:
    """{im_numero: costo estimado por tipo de hilado}, o None si no se pudo.

    Fail-closed igual que la lectura de importaciones: sin el promedio del hilo
    no hay contra qué comparar, y una alarma que no puede leer no inventa.
    """
    try:
        from modules.asinfo import service as asinfo_service
        return asinfo_service.importaciones_costo_estimado(limite=limite) or None
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer el costo por tipo de hilado: %s", e)
        return None


def _esperado(g: dict, costos: dict) -> float | None:
    """Lo que vale el hilo de ESTE grupo, en US$.

    None si a alguna de sus importaciones le falta el histórico de su hilado:
    ahí no se inventa un esperado ni se avisa.
    """
    total = 0.0
    for im in (g.get("ims") or []):
        c = (costos or {}).get(str(im).strip())
        if not c or float(c.get("kg_sin_precio") or 0) > 0:
            return None
        total += float(c.get("costo") or 0)
    return total if total > 0 else None


def _leer_importaciones(limite: int = 1000) -> list[dict] | None:
    """Las importaciones cruzadas, o **None** si no se pudieron leer.

    None y [] no son lo mismo: sin datos no se avisa nada (una alarma que no
    puede leer no inventa), y con esta forma los dos chequeos comparten UNA
    sola lectura por corrida.
    """
    from . import service as svc
    try:
        return svc.importaciones_con_cruce(limite=limite)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer las importaciones: %s", e)
        return None


def _grupos_recibidos(rows: list[dict]) -> dict[str, dict]:
    """Las importaciones RECIBIDAS juntadas por GRUPO de partidas, con toda su
    plata (compras + anticipos) sumada una sola vez.

    Es la base de los dos chequeos de este módulo, y por eso vive aparte: el
    US$/kg fuera de banda y la factura del proveedor cuya plata quedó colgada
    de una sola importación miran los mismos grupos.

    Cada grupo lleva además su `factura` = (código del proveedor en Asinfo +
    nota base). Los miembros de un grupo comparten la nota por construcción
    (es parte de la clave con la que se agrupan), así que alcanza con la del
    primero.
    """
    from . import service as svc

    grupos: dict[str, dict] = {}
    for r in rows or []:
        if not r.get("recibida"):
            continue
        frec = _d(r.get("fecha_recepcion"))
        if not frec:
            continue
        gid = str(r.get("grupo_id") or r.get("im_numero") or "")
        if not gid:
            continue
        g = grupos.setdefault(gid, {
            "grupo_id": gid,
            "codigo": (f"{r.get('prov')} {r.get('numero')}"
                       + (f"-{r.get('numero_hasta')}" if r.get("numero_hasta") else "")),
            "factura": (str(r.get("prov_cod_asinfo") or "").strip().upper(),
                        svc._nota_base(r.get("nota"))),
            "ims": r.get("grupo_ims") or [r.get("im_numero")],
            "kg": float(r.get("grupo_kg") or r.get("kg") or 0),
            "recepcion": frec,
            "importe": 0.0,
            "ids": set(),
        })
        if frec > g["recepcion"]:
            g["recepcion"] = frec        # el grupo está completo con la última
        # El $ se suma UNA vez por compra: las dos mitades de una partida
        # pueden tener colgada la misma compra.
        for it in ((r.get("compra") or {}).get("items") or []):
            _id = it.get("id_compra")
            if _id in g["ids"]:
                continue
            g["ids"].add(_id)
            g["importe"] += float(it.get("importe") or 0)
        if not (r.get("compra") or {}).get("items"):
            for it in ((r.get("anticipo") or {}).get("items") or []):
                g["importe"] += float(it.get("importe") or 0)
    return grupos


def importaciones_fuera_de_banda(dias: int | None = None,
                                 limite: int = 1000,
                                 techo: int | None = None,
                                 rows: list[dict] | None = None,
                                 costos: dict | None = None) -> list[dict]:
    """Grupos recibidos hace más de `dias` cuyo US$/kg sigue fuera de banda.

    Se mira por GRUPO (las dos mitades de una partida son una sola mercadería) y
    se junta TODA la plata: compras + anticipos. Fail-soft: [] si Asinfo no
    contesta — una alarma que no puede leer no inventa.
    """
    from filters import today_ec


    dias = int(dias if dias is not None else _dias_umbral())
    # techo=0 → sin techo (para mirar el histórico a mano; la alarma nunca lo usa)
    techo = _techo_dias() if techo is None else int(techo)
    if rows is None:
        rows = _leer_importaciones(limite=limite)
    if rows is None:
        return []
    if costos is None:
        costos = _leer_costos(limite=limite)
    if not costos:
        return []                    # sin el promedio del hilo no se compara
    hoy = today_ec()

    grupos = _grupos_recibidos(rows)

    out = []
    for g in grupos.values():
        if g["kg"] <= 0:
            continue
        edad = (hoy - g["recepcion"]).days
        if edad < dias:
            continue                     # todavía se está cargando: es normal
        if techo and edad > techo:
            continue                     # historia, no una tarea pendiente
        if not g["ids"] and g["importe"] <= 0:
            # Ni una compra ni un anticipo atribuidos: PC no tiene el dato,
            # no es que Andrés no lo haya cargado. Avisar acá es el ruido que
            # llenó la campanita de 200 avisos el 31/07.
            continue
        esperado = _esperado(g, costos)
        if esperado is None:
            continue                 # ese hilado no tiene histórico: no se inventa
        ratio = g["importe"] / esperado
        if RATIO_MINIMO <= ratio <= RATIO_MAXIMO:
            continue
        ukg = g["importe"] / g["kg"]
        out.append({
            "grupo_id": g["grupo_id"], "codigo": g["codigo"], "ims": g["ims"],
            "kg": round(g["kg"], 2), "importe": round(g["importe"], 2),
            "usd_kg": round(ukg, 4),
            "usd_kg_esperado": round(esperado / g["kg"], 4),
            "esperado": round(esperado, 2), "ratio": round(ratio, 3),
            "recepcion": str(g["recepcion"]),
            "dias": edad, "falta_plata": ratio < RATIO_MINIMO,
            # Cuánto falta para llegar a lo que vale el hilo. Es una
            # referencia, no una cifra a asentar.
            "faltarian_us": round(max(0.0, esperado - g["importe"]), 2),
        })
    out.sort(key=lambda x: -x["dias"])
    return out


# ── La MISMA factura repartida en varias importaciones ──────────────────────
# TMT 2026-08-21 (dueña, sobre MH 68/69/70): *"llegaron varias importaciones
# 68, 69, 70; el pago de 160k era de todo ese hilo. Se registra que llegó pero
# no que valía eso"*.
#
# Asinfo mandó una sola factura del proveedor ("INV HY3821-26") repartida en
# TRES importaciones, cada una con su propio código del programa ( MH 68 ),
# ( MH 69 ) y ( MH 70 ). La compra se carga con el número en el concepto, así
# que los 160.400,78 quedaron colgados de MH 68 sola:
#
#     MH 68   24.300 kg   160.400,78 US$   →  6,6009 US$/kg
#     MH 69   23.430 kg   sin cargar       →  0
#     MH 70   24.150 kg   sin cargar       →  0
#     los tres juntos: 71.880 kg           →  2,2315 US$/kg
#
# Y no es sólo la pantalla. Los 47.580 kg de MH 69 y MH 70 están en la bodega
# SIN plata, y `mov_hilado_valuacion` los saca del divisor a propósito (para
# que un kilo sin dólar no diluya la tarifa): el promedio ponderado toma los
# 160.400,78 enteros contra 24.300 kg. Con ~1,85 millones de kg en stock eso
# levanta la tarifa unos 0,07 US$/kg y revalúa TODO el hilado — del orden de
# 140.000 US$ de utilidad que no son reales, hasta que la plata se acomode.
#
# El caso NO lo agarra la alarma de arriba: recién a los 30 días, y partido en
# tres avisos sueltos que no dicen que son la misma factura.
#
# La regla, y por qué cada pieza está:
#
#   · misma factura del proveedor (`prov_cod_asinfo` + nota base) y todas
#     RECIBIDAS EN EL MISMO MES — es una sola llegada, no dos campañas que
#     reusan el número de factura;
#   · al menos una CON plata y al menos una en CERO — si todas tienen algo, no
#     hay nada mal atribuido; si ninguna tiene, es la alarma de arriba (o PC
#     nunca tuvo el dato) y avisar acá sería el ruido de siempre;
#   · y repartir esos dólares entre los kilos de TODA la factura ACERCA el
#     US$/kg a la banda. Esta sola condición dice las dos cosas que importan:
#     que la plata quedó ARRIBA de la banda para los kilos que tiene colgados
#     (repartir siempre BAJA el US$/kg, así que una que ya está adentro o
#     abajo sólo puede empeorar, y queda afuera sola), y que la mala
#     atribución explica el número. Sin ella saltaría AC 57 —3,55 US$/kg,
#     apenas arriba de la banda pero bien cargada—: repartirla la mandaría a
#     1,77, o sea más lejos, y eso dice que el número alto no es plata de otra.
#
# Por eso NO lleva umbral de días como la alarma de arriba: no está mirando
# "todavía no cargaron la plata" (eso es maduración normal y tarda 10 días de
# mediana), sino "la plata que YA está cargada no le corresponde a esos kilos".
# El techo de antigüedad sí va, por el mismo motivo de siempre: que el día que
# esto se estrene no aparezca un inventario de casos de 2024.
def facturas_con_plata_en_una_sola(limite: int = 1000,
                                   rows: list[dict] | None = None,
                                   techo: int | None = None,
                                   costos: dict | None = None) -> list[dict]:
    """Facturas del proveedor que llegaron en varias importaciones y tienen
    toda la plata colgada de una sola. Fail-soft: [] si no se puede leer."""
    from filters import today_ec


    if rows is None:
        rows = _leer_importaciones(limite=limite)
    if rows is None:
        return []
    if costos is None:
        costos = _leer_costos(limite=limite)
    if not costos:
        return []
    techo = _techo_dias() if techo is None else int(techo)
    hoy = today_ec()

    facturas: dict[tuple, list[dict]] = {}
    for g in _grupos_recibidos(rows).values():
        if g["kg"] <= 0 or not g["factura"][1]:
            continue
        facturas.setdefault(g["factura"], []).append(g)

    out = []
    for (_prov_asinfo, base), miembros in facturas.items():
        if len(miembros) < 2:
            continue                     # una sola importación: no hay reparto
        if len({str(m["recepcion"])[:7] for m in miembros}) > 1:
            continue                     # llegadas de meses distintos
        con = [m for m in miembros if m["importe"] > 0]
        sin = [m for m in miembros if m["importe"] <= 0]
        if not con or not sin:
            continue
        kg_con = sum(m["kg"] for m in con)
        kg_total = sum(m["kg"] for m in miembros)
        us = sum(m["importe"] for m in con)
        if kg_con <= 0 or kg_total <= 0:
            continue
        recepcion = max(m["recepcion"] for m in miembros)
        edad = (hoy - recepcion).days
        if techo and edad > techo:
            continue                     # historia, no una tarea pendiente
        esperado = 0.0
        for m in miembros:
            e = _esperado(m, costos)
            if e is None:
                esperado = 0.0
                break
            esperado += e
        if esperado <= 0:
            continue                 # sin el promedio del hilo no se concluye
        esperado_kg = esperado / kg_total
        ukg = us / kg_con
        ukg_repartido = us / kg_total
        # Repartir esos dólares entre TODA la factura, ¿acerca el US$/kg a lo
        # que vale ese hilo? Si no, el número alto no es plata mal atribuida.
        if abs(ukg_repartido - esperado_kg) >= abs(ukg - esperado_kg):
            # Repartirla no explica mejor el número. Acá caen las que están en
            # banda o abajo: repartir sólo baja el US$/kg, nunca las acerca.
            continue
        orden = sorted(miembros, key=lambda m: m["codigo"])
        out.append({
            "factura": base,
            "codigos": [m["codigo"] for m in orden],
            "con_plata": sorted(m["codigo"] for m in con),
            "sin_plata": sorted(m["codigo"] for m in sin),
            "ims": [i for m in orden for i in (m["ims"] or [])],
            "grupo_ids": sorted(m["grupo_id"] for m in miembros),
            "kg": round(kg_total, 2),
            "kg_con_plata": round(kg_con, 2),
            "kg_sin_plata": round(kg_total - kg_con, 2),
            "importe": round(us, 2),
            "usd_kg": round(ukg, 4),
            "usd_kg_repartido": round(ukg_repartido, 4),
            "usd_kg_esperado": round(esperado_kg, 4),
            "recepcion": str(recepcion),
            "dias": edad,
        })
    out.sort(key=lambda x: -x["kg_sin_plata"])
    return out


# ── La recepción SIN código del programa en la Nota ─────────────────────────
# TMT 2026-08-30 (dueña, sobre MTG3756): *"eso debería haber aparecido en la
# campanita, para que Andrés vea y lo cargue"*.
#
# El 29/08 a las 08:25 Asinfo recibió 16.113,6 kg (IM-653/654) cuya Nota decía
# "MTG3756" pelado, sin el código del programa. Sin código no cruza con ninguna
# compra ni anticipo, así que los kilos entraron al stock SIN su plata y la
# utilidad subió ~48.900 de un saque — y nadie recibió ningún aviso:
#
#   · la alarma de "falta plata" saltea a propósito los grupos sin NADA
#     atribuido (la guarda que evitó los 200 avisos del 31/07), y además
#     espera 30 días;
#   · la de factura repartida necesita una mitad CON plata.
#
# Este caso es distinto de la maduración normal: no es "todavía no cargaron la
# compra" (eso tarda 10 días de mediana y se arregla solo), es "aunque la
# carguen, no va a cruzar NUNCA" — le falta el código en la Nota de Asinfo.
# Por eso avisa de una, sin umbral de días. El techo de antigüedad sí va, como
# siempre, para no estrenar la alarma con un inventario de casos viejos
# (MTGE3755 de enero, la INV 25-26/426 de 2025).
def importaciones_sin_codigo(limite: int = 1000,
                             rows: list[dict] | None = None,
                             techo: int | None = None) -> list[dict]:
    """Recepciones cuya Nota no trae el código del programa (tipo "AC 36").

    Una por factura del proveedor (las partidas ---1/---2 comparten la Nota
    base). Fail-soft: [] si no se pudo leer.
    """
    from filters import today_ec

    from . import service as svc

    if rows is None:
        rows = _leer_importaciones(limite=limite)
    if rows is None:
        return []
    techo = _techo_dias() if techo is None else int(techo)
    hoy = today_ec()

    por_base: dict[str, dict] = {}
    for r in rows or []:
        if not r.get("recibida"):
            continue
        if r.get("prov") and r.get("numero") is not None:
            continue                     # tiene código: no es de acá
        frec = _d(r.get("fecha_recepcion"))
        if not frec:
            continue
        base = svc._nota_base(r.get("nota")) or str(r.get("im_numero") or "")
        if not base:
            continue
        c = por_base.setdefault(base, {
            "nota": base, "ims": [], "kg": 0.0, "recepcion": frec,
            "proveedor": str(r.get("proveedor") or "").strip(),
        })
        c["ims"].append(r.get("im_numero"))
        c["kg"] += float(r.get("kg") or 0)
        if frec > c["recepcion"]:
            c["recepcion"] = frec

    out = []
    for c in por_base.values():
        edad = (hoy - c["recepcion"]).days
        if techo and edad > techo:
            continue                     # historia, no una tarea pendiente
        c["dias"] = edad
        c["kg"] = round(c["kg"], 2)
        c["recepcion"] = str(c["recepcion"])
        out.append(c)
    out.sort(key=lambda x: -x["kg"])
    return out


# ── El aviso que ya se solucionó ────────────────────────────────────────────
# TMT 2026-08-26 (dueña): *"si un aviso ya se solucionó habría que avisar que se
# solucionó"*.
def _resolver_los_arreglados(rows: list[dict] | None) -> None:
    """Los avisos de importaciones que ya se acomodaron pasan a "listo".

    Se resuelve sólo lo VERIFICABLE: el grupo tiene que estar en la lectura de
    HOY y su US$/kg tiene que haber ENTRADO en banda. Que un grupo "ya no
    aparezca" NO alcanza — la lectura trae las últimas 1.000 importaciones y la
    alarma tiene techo de 31 días, así que desaparecer es lo que hacen las
    viejas, no las arregladas.
    """
    if not rows:
        return
    from filters import num_es as _n
    from modules.avisos import queries as avisos

    costos = _leer_costos()
    if not costos:
        return
    grupos = _grupos_recibidos(rows)

    for a in avisos.abiertos_por_clave("import-sin-plata:"):
        clave = str(a.get("clave") or "")
        g = grupos.get(clave.split(":", 1)[1]) if ":" in clave else None
        if not g or g["kg"] <= 0:
            continue
        esperado = _esperado(g, costos)
        if not esperado:
            continue
        if not (RATIO_MINIMO <= g["importe"] / esperado <= RATIO_MAXIMO):
            continue                     # el problema sigue
        avisos.resolver(
            int(a["id_aviso"]),
            titulo=f"{g['codigo']} · listo, ya tiene toda la plata",
            detalle=(f"Quedó en {_n(g['importe'] / g['kg'])} el kilo, y este "
                     f"hilo va a {_n(esperado / g['kg'])}."),
        )

    # La Nota sin código se arregla cuando ALGUNA fila con esa Nota base ya
    # trae el código parseado. "Desaparecer de la lectura" no alcanza (misma
    # regla que arriba): resolver sólo lo verificable.
    from . import service as _svc
    bases_con_codigo = {
        _svc._nota_base(r.get("nota"))
        for r in rows
        if r.get("prov") and r.get("numero") is not None
    }
    for a in avisos.abiertos_por_clave("import-sin-codigo:"):
        clave = str(a.get("clave") or "")
        if ":" not in clave:
            continue
        base = clave.split(":", 1)[1]
        if base not in bases_con_codigo:
            continue                     # sigue sin código (o no se leyó)
        avisos.resolver(
            int(a["id_aviso"]),
            titulo=f"{base} · listo, la Nota ya tiene código",
            detalle="Ya cruza con su compra o anticipo.",
        )

    # La factura repartida se arregla cuando ninguna de sus importaciones
    # quedó sin plata.
    mal = {f["factura"]
           for f in facturas_con_plata_en_una_sola(rows=rows, techo=0,
                                                   costos=costos)}
    for a in avisos.abiertos_por_clave("import-factura-en-una:"):
        clave = str(a.get("clave") or "")
        if ":" not in clave:
            continue
        base = clave.split(":", 1)[1]
        miembros = [g for g in grupos.values() if g["factura"][1] == base]
        if not miembros or base in mal:
            continue
        codigos = ", ".join(sorted(g["codigo"] for g in miembros))
        avisos.resolver(
            int(a["id_aviso"]),
            titulo=f"{codigos} · listo, la plata quedó repartida",
            detalle="Cada importación tiene la suya.",
        )


def revisar_si_toca() -> dict:
    """Corre cada `_FRENO_SECS` y deja los avisos de los dos chequeos: uno por
    grupo fuera de banda y uno por factura repartida. Fail-soft."""
    global _ultima_corrida
    if os.environ.get("IMPORT_SIN_PLATA") == "0":
        return {"corrio": False, "motivo": "apagado"}
    ahora = _t.time()
    with _lock:
        if (ahora - _ultima_corrida) < _FRENO_SECS:
            return {"corrio": False, "motivo": "freno"}
        _ultima_corrida = ahora

    dias = _dias_umbral()
    # UNA sola lectura por corrida para los tres chequeos.
    _rows = _leer_importaciones()
    try:
        casos = importaciones_fuera_de_banda(dias, rows=_rows)
        facturas = facturas_con_plata_en_una_sola(rows=_rows)
        sin_codigo = importaciones_sin_codigo(rows=_rows)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("revisión falló: %s", e)
        return {"corrio": True, "avisados": 0, "error": str(e)[:200]}

    from filters import num_es as _n
    from modules.avisos import queries as avisos

    # ── Cómo se escribe el aviso ────────────────────────────────────────────
    # TMT 2026-08-26 (dueña, sobre la versión larga): *"La importación tiene 2.3
    # por kilo, promedio 2.7. ¿Faltan compras que cargar? Algo así, cortito y
    # fácil de entender"*.
    #
    # Tres decisiones suyas, y valen para los tres avisos de acá:
    #   · el título dice el PRECIO y el precio que debería ser, y termina en la
    #     pregunta. Nada de "el US$/kg quedó fuera de banda";
    #   · el porqué largo —la maduración medida, por qué la tarifa del hilado
    #     queda torcida— NO va en la campanita. Vive en este módulo, que es
    #     donde lo lee el que va a tocar el código;
    #   · sin los IM: *"esos números larguísimos IM-xxxxx es muy largo e
    #     inútil"*. El código del programa (MH 66-67) alcanza para saber de qué
    #     se habla, y desde hoy el "ver →" abre la pantalla YA FILTRADA por él.
    n = 0
    for c in casos:
        if c["falta_plata"]:
            titulo = (f"{c['codigo']} · {_n(c['usd_kg'])} el kilo, y este hilo "
                      f"va a {_n(c['usd_kg_esperado'])}. "
                      "¿Faltan compras por cargar?")
            detalle = (
                f"{_n(c['kg'], 0)} kg con US$ {_n(c['importe'])} cargados: "
                f"faltarían unos US$ {_n(c['faltarian_us'], 0)}. "
                f"Llegó hace {c['dias']} días."
            )
        else:
            # Con la plata cargada, lo que suele faltar son KILOS.
            titulo = (f"{c['codigo']} · {_n(c['usd_kg'])} el kilo, y este hilo "
                      f"va a {_n(c['usd_kg_esperado'])}. ¿Faltan kilos?")
            detalle = (
                f"{_n(c['kg'], 0)} kg con US$ {_n(c['importe'])} cargados. "
                f"Llegó hace {c['dias']} días."
            )
        if avisos.avisar(
            fuente="importaciones",
            nivel="alerta",
            titulo=titulo[:200],
            detalle=detalle,
            importe=c["faltarian_us"] or None,
            url=_url_filtrada(_q_del_caso(c)),
            # Idempotente: se dice UNA vez por grupo, no en cada corrida.
            clave=f"import-sin-plata:{c['grupo_id']}",
        ):
            n += 1
    for f in facturas:
        codigos = ", ".join(f["codigos"])
        con = ", ".join(f["con_plata"])
        titulo = f"{codigos} · toda la plata quedó en {con}"
        detalle = (
            f"Una factura ({f['factura']}) llegó en {len(f['codigos'])} "
            f"importaciones. Los US$ {_n(f['importe'])} cargados quedaron en "
            f"{con}: {_n(f['usd_kg'])} el kilo. Repartidos entre las "
            f"{len(f['codigos'])}, {_n(f['usd_kg_repartido'])}."
        )
        if avisos.avisar(
            fuente="importaciones",
            nivel="alerta",
            titulo=titulo[:200],
            detalle=detalle,
            # La factura del proveedor está adentro de la `nota` de las N
            # importaciones, así que buscarla las trae a las N.
            url=_url_filtrada(str(f["factura"] or "")),
            # Idempotente por FACTURA: se dice una vez, no una por importación.
            clave=f"import-factura-en-una:{f['factura']}",
        ):
            n += 1
    for c in sin_codigo:
        kg_txt = f"{_n(c['kg'], 0)} kg llegaron" if c["kg"] else "llegó mercadería"
        titulo = (f"{c['nota']} · {kg_txt} sin código del programa en la Nota. "
                  "¿Qué compra es?")
        detalle = (
            "Sin el código (tipo AC 36) no cruza con ninguna compra ni "
            "anticipo: los kilos ya están en el stock sin su plata. Ponerle "
            "el código a la Nota en Asinfo y cargar la compra."
        )
        if c.get("proveedor"):
            detalle = f"Proveedor {c['proveedor']}. " + detalle
        if avisos.avisar(
            fuente="importaciones",
            nivel="alerta",
            titulo=titulo[:200],
            detalle=detalle,
            cantidad=len(c.get("ims") or []) or None,
            url=_url_filtrada(c["nota"]),
            # Idempotente por FACTURA del proveedor (las partidas comparten Nota).
            clave=f"import-sin-codigo:{c['nota']}",
        ):
            n += 1
    try:
        _resolver_los_arreglados(_rows)
    except Exception as e:  # noqa: BLE001 -- resolver nunca frena la alarma
        _LOG.warning("no pude resolver los avisos arreglados: %s", e)
    if n:
        _LOG.info("importaciones sin plata: %s aviso(s) nuevos de %s caso(s) "
                  "y %s factura(s) repartida(s)", n, len(casos), len(facturas))
    return {"corrio": True, "casos": len(casos), "facturas": len(facturas),
            "sin_codigo": len(sin_codigo), "avisados": n, "dias": dias}
