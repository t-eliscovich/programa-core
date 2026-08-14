"""Que un error atajado deje rastro.

TMT 2026-08-13 (dueña, después de una tarde entera de bugs que no avisaban):
había 132 bloques que atrapaban una excepción y no hacían absolutamente nada
— ni log, ni aviso. La mayoría son best-effort a propósito (una foto del
saldo, un contador de UI, un snapshot de auditoría) y está bien que no
volteen la pantalla. Lo que no está bien es que, cuando fallan, no quede
NADA: ni vos ni nadie tiene con qué enterarse.

Uso, sin tocar los imports del archivo:

    except Exception as _e:
        from modules._lib.silencios import avisar
        avisar(__name__, "nombre_de_la_funcion", _e)

El import va adentro del `except` a propósito: sólo se paga cuando algo
falló, que por definición es raro, y así no hay que reordenar los imports de
32 archivos para agregar un logger.
"""
from __future__ import annotations

import logging


def avisar(modulo: str, donde: str, e: BaseException, *,
           nivel: str = "warning") -> None:
    """Deja UN renglón en el log y sigue. Nunca levanta.

    Args:
        modulo: `__name__` del archivo que atajó (así el log dice de dónde).
        donde: la función, para no tener que contar líneas.
        e: la excepción atajada.
        nivel: "warning" para los `except Exception` anchos (pueden tapar
            cualquier cosa); "debug" para los tipos angostos de parseo por
            fila, que si no inundan el log con un renglón por renglón.
    """
    try:
        log = logging.getLogger(modulo or "programa_core")
        getattr(log, nivel, log.warning)(
            "silencio en %s: %s: %s", donde, type(e).__name__, e)
    except Exception:  # pragma: no cover - avisar JAMAS puede romper nada
        pass
