"""Foto diaria de la cartera por cliente, tomada por el servidor.

TMT 2026-09-30 (Tamara, análisis de cobranza): la historia de la deuda de
cada cliente no se puede reconstruir después — lo que no se guarda el día
que pasa, se pierde. `scintela.cartera_snapshots` existía desde la 0028 pero
dependía de correr a mano `scripts/tomar_snapshot_cartera.py`, y nadie lo
corría (una sola foto, del 15/05/2026).

Se cuelga del ciclo de fondo (`modules/_lib/autocarga_facturas._loop`),
igual que el resto de las tareas del servidor: no depende de la rutina de
la laptop ni de que alguien abra una pantalla.

La foto del día se REGRABA como máximo una vez por hora: la que queda es la
última del día (la de la noche), que es la que describe cómo terminó el día.
`CARTERA_FOTO_AUTO=0` la apaga.
"""
from __future__ import annotations

import logging
import os

import db
from filters import today_ec

from . import queries

_LOG = logging.getLogger("programa_core.cartera.foto_diaria")

MINUTOS_ENTRE_FOTOS = 60


def toca(ultima_hace_min: float | None) -> bool:
    """¿Hay que (re)tomar la foto de hoy?

    `ultima_hace_min` = minutos desde la última foto de HOY (None si no hay).
    """
    return ultima_hace_min is None or ultima_hace_min >= MINUTOS_ENTRE_FOTOS


def correr_si_toca() -> dict:
    """Toma la foto de hoy si no hay, o si la última tiene más de una hora."""
    if os.environ.get("CARTERA_FOTO_AUTO", "1").strip() == "0":
        return {"corrio": False, "motivo": "apagada"}
    hoy = today_ec()
    fila = db.fetch_one(
        """
        SELECT EXTRACT(EPOCH FROM (LOCALTIMESTAMP - MAX(snapshot_ts))) / 60
               AS minutos
          FROM scintela.cartera_snapshots
         WHERE fecha = %s
        """,
        (hoy,),
    ) or {}
    minutos = fila.get("minutos")
    if not toca(None if minutos is None else float(minutos)):
        return {"corrio": False, "motivo": "reciente"}
    res = queries.tomar_snapshot(hoy)
    _LOG.info("foto de cartera %s: %s clientes", res["fecha"], res["n_clientes"])
    return {"corrio": True, **res}
