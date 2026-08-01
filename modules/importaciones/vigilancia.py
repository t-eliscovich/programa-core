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
import threading
import time as _t
from datetime import date

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


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def importaciones_fuera_de_banda(dias: int | None = None,
                                 limite: int = 1000,
                                 techo: int | None = None) -> list[dict]:
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
    try:
        rows = svc.importaciones_con_cruce(limite=limite)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer las importaciones: %s", e)
        return []
    lo, hi = svc.BANDA_USD_KG
    hoy = today_ec()

    grupos: dict[str, dict] = {}
    for r in rows:
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


def revisar_si_toca() -> dict:
    """Corre cada `_FRENO_SECS` y deja un aviso por grupo. Fail-soft."""
    global _ultima_corrida
    if os.environ.get("IMPORT_SIN_PLATA") == "0":
        return {"corrio": False, "motivo": "apagado"}
    ahora = _t.time()
    with _lock:
        if (ahora - _ultima_corrida) < _FRENO_SECS:
            return {"corrio": False, "motivo": "freno"}
        _ultima_corrida = ahora

    dias = _dias_umbral()
    try:
        casos = importaciones_fuera_de_banda(dias)
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
            url="/importaciones",
            # Idempotente: se dice UNA vez por grupo, no en cada corrida.
            clave=f"import-sin-plata:{c['grupo_id']}",
        ):
            n += 1
    if n:
        _LOG.info("importaciones sin plata: %s aviso(s) nuevos de %s caso(s)",
                  n, len(casos))
    return {"corrio": True, "casos": len(casos), "avisados": n, "dias": dias}
