"""El CATÁLOGO DE COLORES sale de Asinfo — el atributo Color y su padre.

TMT 2026-08-17. Tamara, mirando la ficha del atributo en Asinfo: *"hay algunos
colores que siguen sin mostrarse en facturas proforma… ejemplo de color que no
esta es cocoa, asinfo tambien tiene la categoria. medio bajo etc."*.

La Factura Proforma lista los colores de `scintela.tinto_costos` FILTRADOS por
`clase BETWEEN 1 AND 5`: sin clase no hay fila de la matriz de precios de donde
sacar la cifra, así que el color no se puede cotizar y directamente no aparece.
Al 17/08 quedaban **56 colores activos de Asinfo sin poder cotizarse** (35 que
ni existían en el catálogo y 21 cargados sin clase, COCOA entre ellos).

⭐ **La clase vive en Asinfo**: el atributo `Color` (`atributo.nombre='Color'`)
tiene un valor por color (`valor_atributo.codigo` = el mismo código de 3 letras
que usa esta casa) y cada valor cuelga de un PADRE que es la clase —
BLANCOS / BAJOS / MEDIOS / JASPEADOS / FUERTES. Eso reemplaza a COSTOS.DBF, que
era la única fuente y se retiró el 05/08.

Contra lo cargado coincide en **309 de 313** comparables (98,7%), y los 4 que
no (AMARILLO ORO, AZUL OCEANO, TURQUEZA ESMERALDA, VERDE NAVIDAD) los decidió
Tamara el 17/08: **manda Asinfo**, pasan a MEDIOS.

🚨 **ESPECIALES queda AFUERA** (decisión de Tamara, 17/08): son 62 colores
activos —42 tie dye, 5 jean, 5 pixelado, 4 drill, 3 rayón, camuflage, listado—
y esa clase NO existe en la matriz de precios (que tiene cinco filas, no seis).
Cotizarlos con la fila de otra clase sería inventarles un precio. Se cotizan
aparte, a mano, hasta que tengan su propia fila.

Qué toca y qué no, por color de Asinfo activo cuyo padre es una de las cinco:
  · no está en el catálogo  → ALTA (cod, color, clase, costo 0)
  · está sin clase          → le pone la clase
  · está con OTRA clase     → la corrige (manda Asinfo) y lo reporta
  · `color` vacío           → lo rellena con el nombre de Asinfo
  · `color` ya cargado      → NO se pisa (el catálogo tiene los nombres cortos
    del dBase — "A.ORO" — y pisarlos movería pantallas que no son ésta)
  · `costo` (el $/kg de tintura) → NUNCA se toca: no es de Asinfo.

Corre SOLO, sin cron del EC2: el hilo de fondo de `modules/_lib/autocarga_
facturas.py` llama `correr_si_toca()` y acá se decide, con el MISMO patrón y
las mismas ventanas que el sync de clientes (11:00 y 16:00 Ecuador). El guard
mira el log en base, no la memoria, así un restart no lo repite y una corrida
manual dentro de la ventana también cuenta. `SYNC_COLORES_AUTO=0` lo apaga.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from datetime import UTC, datetime

import db

_LOG = logging.getLogger(__name__)

#: Padre del color en Asinfo → clase de precio de `scintela.precios`.
#: ESPECIALES no está y no es un olvido — ver el docstring.
PADRE_A_CLASE: dict[str, int] = {
    "BLANCOS": 1,
    "BAJOS": 2,
    "MEDIOS": 3,
    "JASPEADOS": 4,
    "FUERTES": 5,
}

#: El padre que Asinfo usa para lo que no entra en la matriz.
PADRE_SIN_MATRIZ = "ESPECIALES"

_SQL_ASINFO = """
SELECT UPPER(LTRIM(RTRIM(COALESCE(v.codigo, ''))))  AS cod,
       LTRIM(RTRIM(COALESCE(v.nombre, '')))         AS nombre,
       UPPER(LTRIM(RTRIM(COALESCE(p.nombre, ''))))  AS padre
  FROM valor_atributo v
  JOIN atributo a
    ON a.id_atributo = v.id_atributo
  LEFT JOIN valor_atributo p
    ON p.id_valor_atributo = v.id_valor_atributo_padre
 WHERE a.nombre = 'Color'
   AND v.activo = 1
"""

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.colores_sync_asinfo_log (
    id_corrida  SERIAL PRIMARY KEY,
    corrido     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usuario     VARCHAR(60),
    ok          BOOLEAN NOT NULL,
    resumen     TEXT
)
"""

#: `tinto_costos.cod` es varchar(5) y `color` varchar(30) — recortar acá y no
#: dejar que Postgres tire el INSERT entero por un nombre largo de Asinfo.
_MAX_COD = 5
_MAX_COLOR = 30


def asegurar_tabla() -> None:
    db.execute(_BOOTSTRAP_SQL)


def traer_asinfo() -> tuple[list[dict], bool]:
    """Los colores activos del atributo Color. `(filas, contestó)`.

    `contestó=False` = Metabase caído o sin config, que NO es lo mismo que
    "Asinfo no tiene colores". Con False no se toca nada — si no, una caída
    del puente se leería como catálogo vacío.
    """
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return [], False
    filas, contesto = mc.fetch_dataset_estado(2, _SQL_ASINFO, max_results=20000)
    return list(filas or []), bool(contesto)


def sincronizar(usuario: str = "sync-colores") -> dict:
    """Un pase completo, idempotente. Nunca levanta: devuelve el reporte.

    Reporte: {ok, leidos, altas: [cod], con_clase: [cod], reclasificados:
    [{cod, color, de, a}], nombres, especiales, sin_padre: [cod], error?}.
    """
    try:
        return _sincronizar(usuario)
    except Exception as e:  # noqa: BLE001 — el hilo de fondo no puede morir acá
        _LOG.exception("sync colores Asinfo murió")
        reporte = {"ok": False, "error": str(e)[:300]}
        _guardar_log(usuario, reporte)
        return reporte


def _sincronizar(usuario: str) -> dict:
    filas, contesto = traer_asinfo()
    if not contesto:
        reporte = {"ok": False,
                   "error": "Asinfo no contestó (Metabase). No se tocó nada."}
        _guardar_log(usuario, reporte)
        return reporte

    catalogo = {
        (r["cod"] or "").strip().upper(): r
        for r in (db.fetch_all(
            "SELECT UPPER(TRIM(cod)) AS cod, COALESCE(color, '') AS color, clase "
            "FROM scintela.tinto_costos WHERE cod IS NOT NULL AND TRIM(cod) <> ''"
        ) or [])
    }

    altas: list[str] = []
    con_clase: list[str] = []
    reclasificados: list[dict] = []
    nombres = 0
    especiales = 0
    sin_padre: list[str] = []
    leidos = 0

    for fila in filas:
        cod = (fila.get("cod") or "").strip().upper()[:_MAX_COD]
        if not cod:
            continue
        leidos += 1
        padre = (fila.get("padre") or "").strip().upper()
        clase = PADRE_A_CLASE.get(padre)
        if clase is None:
            if padre == PADRE_SIN_MATRIZ:
                especiales += 1
            else:
                sin_padre.append(cod)
            continue
        nombre = (fila.get("nombre") or "").strip().upper()[:_MAX_COLOR]

        actual = catalogo.get(cod)
        if actual is None:
            db.execute(
                "INSERT INTO scintela.tinto_costos "
                "(cod, color, costo, clase, usuario_crea) "
                "VALUES (%s, %s, 0, %s, %s) ON CONFLICT (cod) DO NOTHING",
                (cod, nombre, clase, usuario[:50]),
            )
            altas.append(cod)
            continue

        sets: list[str] = []
        params: list = []
        clase_hoy = actual.get("clase")
        clase_hoy = int(clase_hoy) if clase_hoy in (1, 2, 3, 4, 5) else None
        if clase_hoy is None:
            sets.append("clase=%s")
            params.append(clase)
            con_clase.append(cod)
        elif clase_hoy != clase:
            # Manda Asinfo (Tamara 17/08). Se corrige Y se reporta: es un
            # cambio de PRECIO, no un detalle de catálogo.
            sets.append("clase=%s")
            params.append(clase)
            reclasificados.append({
                "cod": cod,
                "color": (actual.get("color") or nombre),
                "de": clase_hoy,
                "a": clase,
            })
        # El nombre sólo se RELLENA: los del catálogo son los cortos del dBase
        # y pisarlos movería pantallas que no son ésta.
        if not (actual.get("color") or "").strip() and nombre:
            sets.append("color=%s")
            params.append(nombre)
            nombres += 1
        if sets:
            db.execute(
                f"UPDATE scintela.tinto_costos SET {', '.join(sets)}, "
                "fecha_modifica=CURRENT_TIMESTAMP, usuario_modifica=%s "
                "WHERE UPPER(TRIM(cod))=%s",
                (*params, usuario[:50], cod),
            )

    reporte = {
        "ok": True,
        "leidos": leidos,
        "altas": sorted(altas),
        "con_clase": sorted(con_clase),
        "reclasificados": reclasificados,
        "nombres": nombres,
        "especiales": especiales,
        "sin_padre": sorted(set(sin_padre)),
    }
    _limpiar_cache()
    _guardar_log(usuario, reporte)
    return reporte


def _limpiar_cache() -> None:
    """El bloque de /precios cachea 5 min los colores a revisar: si el sync
    acaba de completar 56, no puede seguir mostrándolos como pendientes."""
    try:
        from modules.precios import queries as _q

        _q._COLORES_CACHE.clear()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude limpiar la caché de colores: %s", e)


def frase(reporte: dict) -> str:
    """Una línea para el flash y para el log. Dice lo que CAMBIÓ."""
    if not reporte.get("ok"):
        return reporte.get("error") or "No se pudo sincronizar."
    partes = []
    if reporte.get("altas"):
        partes.append(f"{len(reporte['altas'])} color(es) nuevo(s)")
    if reporte.get("con_clase"):
        partes.append(f"{len(reporte['con_clase'])} ya se pueden cotizar")
    if reporte.get("reclasificados"):
        partes.append(f"{len(reporte['reclasificados'])} cambiaron de clase")
    esp = reporte.get("especiales") or 0
    cola = (f" {esp} ESPECIALES quedan afuera (tie dye, jean, drill, rayón): "
            "esa clase no está en la lista de precios.") if esp else ""
    if not partes:
        return "El catálogo de colores ya estaba al día con Asinfo." + cola
    return " · ".join(partes) + "." + cola


def _guardar_log(usuario: str, reporte: dict) -> None:
    try:
        asegurar_tabla()
        db.execute(
            "INSERT INTO scintela.colores_sync_asinfo_log (usuario, ok, resumen) "
            "VALUES (%s, %s, %s)",
            (usuario[:60], bool(reporte.get("ok")), json.dumps(reporte)[:8000]),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude guardar el log del sync de colores: %s", e)


def ultimas_corridas(limite: int = 10) -> list[dict]:
    try:
        asegurar_tabla()
        rows = db.fetch_all(
            "SELECT corrido, usuario, ok, resumen "
            "FROM scintela.colores_sync_asinfo_log "
            "ORDER BY id_corrida DESC LIMIT %s",
            (limite,),
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer el log del sync de colores: %s", e)
        return []
    for r in rows:
        try:
            r["reporte"] = json.loads(r.pop("resumen") or "{}")
        except (ValueError, TypeError):
            r["reporte"] = {}
    return rows


# ---------------------------------------------------------------------------
# Corre SOLO — mismas ventanas que el sync de clientes (11:00 y 16:00 EC).
# ---------------------------------------------------------------------------
_VENTANAS_UTC = (16, 21)  # 11:00 y 16:00 Ecuador (UTC-5)
_CHECK_MIN_SECS = 300     # mirar el log a lo sumo cada 5 min
_auto_lock = threading.Lock()
_auto_ultimo_check = 0.0


def _inicio_ventana_utc(ahora_utc: datetime) -> datetime | None:
    """El comienzo de la ventana vigente, o None si todavía no abrió ninguna."""
    inicio = None
    for h in _VENTANAS_UTC:
        cand = ahora_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand <= ahora_utc:
            inicio = cand
    return inicio


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    res: dict = {"corrio": False}
    if os.environ.get("SYNC_COLORES_AUTO", "1") == "0":
        return res
    global _auto_ultimo_check
    ahora_mono = _time.monotonic()
    with _auto_lock:
        if _auto_ultimo_check and (ahora_mono - _auto_ultimo_check) < _CHECK_MIN_SECS:
            return res
        _auto_ultimo_check = ahora_mono
    try:
        ahora_utc = datetime.now(UTC)
        inicio = _inicio_ventana_utc(ahora_utc)
        if inicio is None:
            return res
        asegurar_tabla()
        ya = db.fetch_one(
            "SELECT 1 AS x FROM scintela.colores_sync_asinfo_log "
            "WHERE ok AND corrido >= %s LIMIT 1",
            (inicio.replace(tzinfo=None),),
        )
        if ya:
            return res
        res["corrio"] = True
        res["reporte"] = sincronizar(usuario="auto-sync-colores")
    except Exception as e:  # noqa: BLE001 — el hilo no se cae por esto
        _LOG.warning("sync colores (fondo): %s", e)
    return res
