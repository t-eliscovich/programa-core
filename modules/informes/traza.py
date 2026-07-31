"""La grabadora de la utilidad: una foto cada pocos minutos.

TMT 2026-07-31 (dueña): *"mirálo ahora. guardá la data y fijate en un rato
también. no podemos tener utilidad menor a la mañana"*.

Todo el día se discutió por qué la utilidad se movía y cada respuesta fue una
hipótesis, porque no había con qué comparar: `scintela.historia` guarda UNA fila
por día, así que un salto de las 09:16 a las 10:34 no queda registrado en ningún
lado. Esto lo registra.

Cada vuelta del ciclo de fondo, si pasaron 5 minutos desde la última, guarda la
utilidad JUNTO CON sus componentes. Cuando salta, se mira la fila anterior y se
ve QUÉ se movió — no se adivina.

Reglas:

· **Nadie depende de esto.** Es append-only y el balance no la lee. Si falla,
  se loguea y la app sigue igual.
· **Kilos y tarifa por separado.** El stock se valúa a promedio ponderado: la
  tarifa mueve el valor de TODO el stock de un saque, así que hay que poder
  distinguir "entraron kilos" de "cambió el $/kg".
· **Hora de Ecuador** para mostrar, como en Novedades y en el aviso de ventas.
"""
from __future__ import annotations

import logging
import os
import time

import db

_LOG = logging.getLogger("programa_core.traza_utilidad")

#: Cada cuánto se guarda una foto. 5 minutos = el TTL de las cachés de Asinfo,
#: así que cada foto ve datos nuevos y no repetimos filas idénticas.
INTERVALO_SECS = 300

_ultimo_ts: float = 0.0


def _intervalo() -> int:
    try:
        n = int(os.environ.get("TRAZA_UTILIDAD_SECS", str(INTERVALO_SECS)))
    except (TypeError, ValueError):
        return INTERVALO_SECS
    return n if 30 <= n <= 3600 else INTERVALO_SECS


def _f(d: dict, *claves):
    """Primer valor numérico presente entre `claves` (o None)."""
    for k in claves:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _fila_desde_balance(bal: dict) -> dict:
    """Aplana el balance a las columnas de la traza."""
    bal = bal or {}
    comp = (bal.get("diagnostico") or {}).get("componentes") or {}
    etapas = bal.get("stock_etapas") or {}
    hilado = etapas.get("hilado") or {}
    tejido = etapas.get("tejido") or {}
    term = etapas.get("terminado") or {}
    kg = bal.get("kg") or {}
    hil = bal.get("hilado_valuacion") or {}
    return {
        "utilidad": _f(comp, "utilidad"),
        "patr_neto": (
            (_f(comp, "patr") or 0) - (_f(comp, "uret") or 0)
            if comp.get("patr") is not None else None
        ),
        "caja": _f(comp, "salcaj"),
        "bancos": _f(comp, "salbanc_total"),
        "cheques": _f(comp, "totc"),
        "facturas": _f(comp, "totf"),
        "antic": _f(comp, "antic"),
        "vsto": _f(comp, "vsto"),
        "vqx": _f(comp, "vqx"),
        "umaq": _f(comp, "umaq"),
        "uact": _f(comp, "uact"),
        "totp": _f(comp, "totp"),
        "uret": _f(comp, "uret"),
        "hilado_kg": _f(hilado, "kg"),
        "hilado_ukg": _f(hilado, "ukg"),
        "tejido_kg": _f(tejido, "kg"),
        "terminado_kg": _f(term, "kg"),
        "compras_kg": _f(hil, "compras"),
        "compras_us": _f(hil, "compras_us"),
        "kg_sin_costo": _f(hil, "kg_sin_costo"),
        "venta_kg": _f(kg, "kvent"),
        "venta_us": _f(kg, "uvent"),
    }


def registrar(origen: str = "manual", bal: dict | None = None) -> bool:
    """Guarda UNA foto. True si se insertó. Nunca lanza."""
    try:
        if bal is None:
            from modules.informes import queries as _q
            bal = _q.informe_balance()
        fila = _fila_desde_balance(bal)
        if fila.get("utilidad") is None:
            return False                      # balance sin componentes: no sirve
        fila["origen"] = (origen or "manual")[:20]
        cols = list(fila.keys())
        db.execute(
            "INSERT INTO scintela.traza_utilidad (%s) VALUES (%s)" % (
                ", ".join(cols), ", ".join(f"%({c})s" for c in cols)),
            fila,
        )
        return True
    except Exception as e:  # noqa: BLE001 -- la grabadora no puede tumbar nada
        _LOG.warning("traza_utilidad: no se pudo guardar la foto (%s)", e)
        return False


def registrar_si_toca(origen: str = "loop") -> bool:
    """Llamada desde el ciclo de fondo: guarda si pasó el intervalo."""
    global _ultimo_ts
    ahora = time.time()
    if (ahora - _ultimo_ts) < _intervalo():
        return False
    _ultimo_ts = ahora
    return registrar(origen=origen)


# ── Lectura ────────────────────────────────────────────────────────────────

#: Columnas que se comparan foto contra foto para decir "esto fue lo que se
#: movió". `totp` va con el signo dado vuelta porque es pasivo: si sube, la
#: utilidad baja.
COMPONENTES = (
    ("caja", 1), ("bancos", 1), ("cheques", 1), ("facturas", 1),
    ("antic", 1), ("vsto", 1), ("vqx", 1), ("umaq", 1), ("uact", 1),
    ("totp", -1), ("uret", 1),
)

ETIQUETAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheques",
    "facturas": "Facturas", "antic": "Anticipos", "vsto": "Stock MP+Prod.",
    "vqx": "Stock Químicos", "umaq": "Maquinaria", "uact": "Terrenos/Edif.",
    "totp": "Pasivos", "uret": "Dividendos",
}


def ultimas(n: int = 120) -> list[dict]:
    """Últimas n fotos, la más nueva primero, con la hora de Ecuador."""
    try:
        n = max(1, min(1000, int(n)))
    except (TypeError, ValueError):
        n = 120
    try:
        return db.fetch_all(
            f"""
            SELECT *,
                   TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil',
                           'DD/MM HH24:MI') AS cuando
              FROM scintela.traza_utilidad
             ORDER BY creado_en DESC
             LIMIT {n}
            """
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no se pudo leer (%s)", e)
        return []


def con_deltas(filas: list[dict]) -> list[dict]:
    """Agrega a cada foto el Δ contra la ANTERIOR (la de abajo en la lista) y
    quién explica ese Δ.

    `filas` viene de `ultimas()`: más nueva primero. La última no tiene contra
    qué compararse y queda con delta None.
    """
    out = []
    for i, f in enumerate(filas or []):
        fila = dict(f)
        prev = filas[i + 1] if (i + 1) < len(filas) else None
        if not prev:
            fila["d_utilidad"] = None
            fila["movio"] = []
            out.append(fila)
            continue
        try:
            fila["d_utilidad"] = round(
                float(f.get("utilidad") or 0) - float(prev.get("utilidad") or 0), 2)
        except (TypeError, ValueError):
            fila["d_utilidad"] = None
        movs = []
        for col, signo in COMPONENTES:
            try:
                d = float(f.get(col) or 0) - float(prev.get(col) or 0)
            except (TypeError, ValueError):
                continue
            if abs(d) >= 1:                    # el ruido de centavos no interesa
                movs.append({
                    "col": col, "label": ETIQUETAS.get(col, col),
                    "delta": round(d, 2),
                    "aporte": round(d * signo, 2),
                })
        movs.sort(key=lambda m: abs(m["aporte"]), reverse=True)
        fila["movio"] = movs
        out.append(fila)
    return out


def bajadas(filas: list[dict], umbral: float = 1000.0) -> list[dict]:
    """Las fotos en las que la utilidad BAJÓ más que `umbral`.

    Dueña: *"no podemos tener utilidad menor a la mañana"*. Esto es la lista de
    veces que pasó, con el componente que lo explica.
    """
    return [f for f in (filas or [])
            if f.get("d_utilidad") is not None and f["d_utilidad"] <= -abs(umbral)]
