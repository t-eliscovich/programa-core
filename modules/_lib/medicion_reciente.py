"""La última medición de un chequeo lento, para que /admin/health/all no la repita.

Tamara 2026-10-07: /admin/health/all "se cuelga". No se colgaba: tardaba
~80 s (medido en vivo), y el navegador de la rutina diaria se rendía a los
45-60 s. Dos chequeos contra Asinfo se llevaban casi todo:
`salidas_sin_saldo` (~48 s) y `acabado_pedidos` (~38 s). Los dos ya corren
solos en el hilo de fondo, así que el health/all usa lo que midió el hilo si
es reciente, en vez de volver a preguntarle a Asinfo. Las rutas sueltas
(/admin/health/salidas-sin-saldo, /admin/health/acabado-pedidos) siguen
midiendo en el momento.
"""
from __future__ import annotations

import copy
import threading
import time


class MedicionReciente:
    """Guarda el último resultado `{ok, alerts, stats}` y cuándo se midió."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cuando: float | None = None
        self._res: dict | None = None

    def guardar(self, res: dict) -> None:
        if (res.get("stats") or {}).get("sin_datos"):
            return  # Asinfo no contestó: que la próxima vuelva a preguntar
        with self._lock:
            self._cuando = time.monotonic()
            self._res = copy.deepcopy(res)

    def leer(self, max_edad_secs: int) -> dict | None:
        """El resultado guardado si tiene menos de `max_edad_secs`, con
        `stats.medido_hace_min`; si no hay o es viejo, None."""
        with self._lock:
            if self._cuando is None or self._res is None:
                return None
            edad = time.monotonic() - self._cuando
            if edad > max_edad_secs:
                return None
            res = copy.deepcopy(self._res)
        res.setdefault("stats", {})["medido_hace_min"] = int(edad // 60)
        return res

    def olvidar(self) -> None:
        with self._lock:
            self._cuando = None
            self._res = None
