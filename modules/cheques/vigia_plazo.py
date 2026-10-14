"""Vigía: cheques vivos con la fecha de depósito a más de 360 días.

Tamara 13/10/2026: *"que no se pueda pagar a más de 360"* y, sobre el cheque
3616 de GUG (cargado el 13/10/2026 a depositar el 31/12/2027), *"ponele
campanita a Alex"*. El freno de `queries.validar_plazo` ataja lo que se carga
de acá en adelante; esto avisa los que YA están cargados así, y da vuelta su
propio aviso cuando alguien corrige la fecha (regla del 26/08: el vigía dice
"listo" solo).

Corre en el hilo del calentador (`warmup._loop`), como mucho una vez cada
`_CADA_S`. Fail-soft: nunca lanza.
"""
from __future__ import annotations

import logging
import time

import db

from .queries import PLAZO_MAX_DIAS, SQL_DIA_INGRESO

_LOG = logging.getLogger(__name__)

_CADA_S = 600.0
_ultima = 0.0
FUENTE = "cheques"
PREFIJO = "plazo:"


#: Desde cuándo se cuentan los días: un cheque POSTERGADO cuenta desde el día
#: en que se postergó (igual que el freno de `postergar`), no desde que entró.
#: 13/10/2026: la primera vuelta avisó 8 postergados viejos de MSJ, WUA, FRI
#: y JHM (cargados en 2022–2024, postergados en septiembre) — falsa alarma.
_DESDE = (f"COALESCE(c.fecha_postergacion::date, {SQL_DIA_INGRESO}, c.fecha)")


def fuera_de_plazo() -> list[dict]:
    """Cheques en cartera (Z/P) a depositar a más de PLAZO_MAX_DIAS de su ingreso."""
    return db.fetch_all(
        f"""
        SELECT c.id_cheque, c.no_cheque, UPPER(TRIM(c.codigo_cli)) AS cod,
               c.importe, c.fechad,
               c.fechad - {_DESDE} AS dias
          FROM scintela.cheque c
         WHERE UPPER(TRIM(COALESCE(c.stat, ''))) IN ('Z', 'P')
           AND COALESCE(c.no_banco, 0) <> 98
           AND c.fechad - {_DESDE} > %s
        """, (PLAZO_MAX_DIAS,)) or []


def _titulo(r: dict) -> str:
    return (f"Cheque {(r.get('no_cheque') or '').strip()} de {r.get('cod') or ''} "
            f"a depositar el {r['fechad'].strftime('%d/%m/%Y')}: {r['dias']} días")


def _fecha_sigue_igual(aviso: dict) -> bool:
    """La fecha del título del aviso es la que el cheque tiene hoy."""
    try:
        id_cheque = int(aviso["clave"][len(PREFIJO):])
        r = db.fetch_one("SELECT fechad FROM scintela.cheque WHERE id_cheque = %s",
                         (id_cheque,))
        return bool(r and r.get("fechad")
                    and r["fechad"].strftime("%d/%m/%Y") in (aviso.get("titulo") or ""))
    except Exception:  # noqa: BLE001
        return False


def vigilar(forzar: bool = False) -> dict:
    """Avisa los nuevos y da vuelta los que ya se corrigieron."""
    global _ultima
    if not forzar and time.monotonic() - _ultima < _CADA_S:
        return {}
    _ultima = time.monotonic()
    res = {"avisados": 0, "resueltos": 0}
    try:
        from modules.avisos.queries import archivar, avisar, resolver

        malos = fuera_de_plazo()
        for r in malos:
            if avisar(fuente=FUENTE, nivel="alerta", titulo=_titulo(r),
                      detalle=(f"Pasa el máximo de {PLAZO_MAX_DIAS} días desde que "
                               "entró: revisá el año y corregí la fecha desde la "
                               "ficha del cheque."),
                      importe=r.get("importe"), url=f"/cheques/{r['id_cheque']}",
                      clave=f"{PREFIJO}{r['id_cheque']}"):
                res["avisados"] += 1
        vivos = {f"{PREFIJO}{r['id_cheque']}" for r in malos}
        abiertos = db.fetch_all(
            "SELECT id_aviso, clave, titulo FROM scintela.aviso "
            " WHERE fuente = %s AND clave LIKE %s AND nivel <> 'ok' "
            "   AND NOT COALESCE(archivado, FALSE)",
            (FUENTE, PREFIJO + "%")) or []
        for a in abiertos:
            if a["clave"] in vivos:
                continue
            tit = (a.get("titulo") or "").split(" a depositar")[0]
            if _fecha_sigue_igual(a):
                # La fecha no cambió: el aviso fue de más (se cambió la
                # regla), no se corrigió nada. Se archiva sin decir "listo".
                if archivar(a["id_aviso"], "vigia-plazo"):
                    res["archivados"] = res.get("archivados", 0) + 1
            elif resolver(a["id_aviso"], titulo=f"{tit}: fecha corregida"):
                res["resueltos"] += 1
    except Exception as e:  # noqa: BLE001 -- un vigía no rompe el hilo
        _LOG.warning("vigía de plazo de cheques falló: %s", e)
    return res
