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


def _dist_banda(ukg: float, lo: float, hi: float) -> float:
    """Cuán lejos está un US$/kg de la banda razonable (0 = adentro)."""
    if lo <= ukg <= hi:
        return 0.0
    return min(abs(ukg - lo), abs(ukg - hi))


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
                                 rows: list[dict] | None = None) -> list[dict]:
    """Grupos recibidos hace más de `dias` cuyo US$/kg sigue fuera de banda.

    Se mira por GRUPO (las dos mitades de una partida son una sola mercadería) y
    se junta TODA la plata: compras + anticipos. Fail-soft: [] si Asinfo no
    contesta — una alarma que no puede leer no inventa.
    """
    from filters import today_ec

    from . import service as svc

    dias = int(dias if dias is not None else _dias_umbral())
    # techo=0 → sin techo (para mirar el histórico a mano; la alarma nunca lo usa)
    techo = _techo_dias() if techo is None else int(techo)
    if rows is None:
        rows = _leer_importaciones(limite=limite)
    if rows is None:
        return []
    lo, hi = svc.BANDA_USD_KG
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
        ukg = g["importe"] / g["kg"]
        if lo <= ukg <= hi:
            continue
        out.append({
            "grupo_id": g["grupo_id"], "codigo": g["codigo"], "ims": g["ims"],
            "kg": round(g["kg"], 2), "importe": round(g["importe"], 2),
            "usd_kg": round(ukg, 4), "recepcion": str(g["recepcion"]),
            "dias": edad, "falta_plata": ukg < lo,
            # Cuánto habría que cargar para que el grupo entre en banda por
            # abajo. Es una referencia, no una cifra a asentar.
            "faltarian_us": round(max(0.0, g["kg"] * lo - g["importe"]), 2),
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
                                   techo: int | None = None) -> list[dict]:
    """Facturas del proveedor que llegaron en varias importaciones y tienen
    toda la plata colgada de una sola. Fail-soft: [] si no se puede leer."""
    from filters import today_ec

    from . import service as svc

    if rows is None:
        rows = _leer_importaciones(limite=limite)
    if rows is None:
        return []
    lo, hi = svc.BANDA_USD_KG
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
        ukg = us / kg_con
        ukg_repartido = us / kg_total
        if _dist_banda(ukg_repartido, lo, hi) >= _dist_banda(ukg, lo, hi):
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
            "recepcion": str(recepcion),
            "dias": edad,
        })
    out.sort(key=lambda x: -x["kg_sin_plata"])
    return out


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
    # UNA sola lectura por corrida para los dos chequeos.
    _rows = _leer_importaciones()
    try:
        casos = importaciones_fuera_de_banda(dias, rows=_rows)
        facturas = facturas_con_plata_en_una_sola(rows=_rows)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("revisión falló: %s", e)
        return {"corrio": True, "avisados": 0, "error": str(e)[:200]}

    from modules.avisos import queries as avisos
    from modules.importaciones import service as svc
    lo, hi = svc.BANDA_USD_KG

    n = 0
    for c in casos:
        if c["falta_plata"]:
            titulo = (f"{c['codigo']} · llegó hace {c['dias']} días y le falta "
                      f"plata por cargar")
            detalle = (
                f"Recibida el {c['recepcion']}: {c['kg']:,.0f} kg, y hasta hoy "
                f"tiene cargados US$ {c['importe']:,.2f} — {c['usd_kg']:,.4f} "
                f"US$/kg, contra una banda normal de {lo:,.1f}–{hi:,.1f}. "
                f"Para entrar en banda faltarían unos US$ {c['faltarian_us']:,.0f} "
                "(factura, CAE, flete o seguro).\n\n"
                "Una importación tarda normalmente 10 días en tener toda su "
                f"plata, y 34 de cada 35 cierran antes de los 21. Ésta lleva "
                f"{c['dias']}.\n\n"
                "Ojo: también puede ser que la plata ESTÉ cargada y el sistema "
                "no se la esté atribuyendo — vale la pena mirar las dos cosas. "
                f"Importación {', '.join(str(i) for i in c['ims'])}."
            )
        else:
            titulo = (f"{c['codigo']} · llegó hace {c['dias']} días y el US$/kg "
                      f"quedó alto ({c['usd_kg']:,.2f})")
            detalle = (
                f"Recibida el {c['recepcion']}: {c['kg']:,.0f} kg por "
                f"US$ {c['importe']:,.2f} = {c['usd_kg']:,.4f} US$/kg, contra una "
                f"banda normal de {lo:,.1f}–{hi:,.1f}. Con la plata ya cargada, "
                "lo que suele faltar son KILOS — que la importación esté partida "
                "en dos documentos y falte uno, o que tenga pegada plata de otra. "
                f"Importación {', '.join(str(i) for i in c['ims'])}."
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
        titulo = (f"{codigos} · la misma factura llegó en {len(f['codigos'])} "
                  "importaciones y la plata quedó en una sola")
        detalle = (
            f"La factura {f['factura']} del proveedor llegó en "
            f"{len(f['codigos'])} importaciones ({codigos}): {f['kg']:,.0f} kg "
            f"en total, recibidas el {f['recepcion']}.\n\n"
            f"Los US$ {f['importe']:,.2f} que hay cargados están colgados de "
            f"{', '.join(f['con_plata'])} sola: {f['kg_con_plata']:,.0f} kg = "
            f"{f['usd_kg']:,.4f} US$/kg, contra una banda normal de "
            f"{lo:,.1f}–{hi:,.1f}. Repartidos entre los {f['kg']:,.0f} kg de "
            f"toda la factura darían {f['usd_kg_repartido']:,.4f}.\n\n"
            f"Los {f['kg_sin_plata']:,.0f} kg de {', '.join(f['sin_plata'])} "
            "están en la bodega sin plata, y esos kilos NO diluyen la tarifa "
            "del hilado: el promedio ponderado toma los dólares enteros contra "
            "los kilos que sí la tienen, así que la tarifa —y con ella el "
            "stock— quedan altos hasta que esto se acomode.\n\n"
            "Se acomoda de dos maneras: que la nota de Asinfo diga el código "
            f"con el rango en las {len(f['codigos'])} (como ya pasa con "
            "MH 64-65, y el programa las junta solas), o cargar la plata que "
            "le toca a cada una.\n\n"
            f"Importación {', '.join(str(i) for i in f['ims'])}."
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
    if n:
        _LOG.info("importaciones sin plata: %s aviso(s) nuevos de %s caso(s) "
                  "y %s factura(s) repartida(s)", n, len(casos), len(facturas))
    return {"corrio": True, "casos": len(casos), "facturas": len(facturas),
            "avisados": n, "dias": dias}
