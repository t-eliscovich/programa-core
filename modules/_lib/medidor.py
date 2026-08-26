"""El termómetro de las pantallas: cuánto tardó cada una y cuántas consultas hizo.

TMT 2026-08-26 (dueña): *"cómo se podría evaluar las pantallas del programa y
hacerlas más rápido"*.

⭐ POR QUÉ EXISTE, que es una lección de este mismo día

Pasé una sesión entera midiendo pantallas en una base LOCAL sembrada a ojo, y
me equivoqué dos veces: primero con la escala (sembré 60.000 cheques cuando la
fábrica tiene ~4.500) y después con la conclusión (le dije a la dueña que
comisiones estaba lento cuando ya se había arreglado en agosto, otro problema).
Las dos veces el error fue el mismo: **adivinar producción desde afuera**.

El programa ya sabía la respuesta y no se la preguntaba nadie. `app._log_request`
mide cada request desde 2026 y lo escribe en un log que vive en el servidor
Windows; `db._t` mide cada consulta y hace lo mismo. Nadie entra al servidor a
leer esos logs, así que el dato existía y no servía.

Esto junta las dos mediciones —los milisegundos del request y las consultas que
hizo— y las deja a mano en una pantalla. La pregunta *"¿qué pantalla está lenta
de verdad?"* pasa a tener una respuesta que no depende de mi laboratorio.

⚠ VIVE EN MEMORIA, A PROPÓSITO

No hay tabla, no hay migración y no se escribe un solo renglón en la base. Un
contador que escribe en la base en cada request es una forma elegante de hacer
más lento justo lo que se quería medir. El precio es que se borra en cada
deploy, y está bien: lo que se busca es *"qué está lento HOY"*, no una serie
histórica. Si algún día hace falta la serie, lo que se guarda es la foto diaria,
no cada visita.

⚠ Y NO GUARDA NADA DE NADIE: la ruta con sus `<id>` sin reemplazar
(`/facturas/<numf>`, no `/facturas/10879`), los milisegundos y el texto del SQL
—que es código, no datos—. Ni usuario, ni parámetros, ni valores.
"""

from __future__ import annotations

import statistics
import threading
import time

#: Cuántas rutas distintas se recuerdan. La app tiene ~270 pantallas; el techo
#: está para que un bicho que genere rutas infinitas no se coma la memoria.
MAX_RUTAS = 500

#: Cuántas visitas se guardan por ruta para poder sacar la mediana. Con las
#: últimas 30 alcanza y son 240 bytes por pantalla.
MUESTRAS_POR_RUTA = 30

#: Arriba de esto una visita se guarda aparte, con su peor consulta. Es el
#: mismo umbral que ya usa el log de requests (`REQ_SLOW_MS`).
LENTA_MS = 500.0

#: Cuántas visitas lentas se recuerdan, las más recientes.
MAX_LENTAS = 40

_LOCAL = threading.local()
_LOCK = threading.Lock()
_RUTAS: dict[str, dict] = {}
_LENTAS: list[dict] = []
_DESDE = time.time()


# ── lo que va contando el request en curso ──────────────────────────────────
# Un thread-local y no `flask.g` a propósito: `db._t` también corre desde los
# hilos de fondo (el calentador, la auto-carga de facturas), donde no hay
# request. Ahí esto no cuenta nada en vez de explotar.


def arrancar() -> None:
    """Empieza a contar. La llama el `before_request` de la app."""
    _LOCAL.activo = True
    _LOCAL.n = 0
    _LOCAL.ms = 0.0
    _LOCAL.peor_ms = 0.0
    _LOCAL.peor_sql = ""


def anotar_consulta(ms: float, sql: str) -> None:
    """Una consulta que terminó. La llama `db._t`, que ya las mide todas."""
    if not getattr(_LOCAL, "activo", False):
        return
    _LOCAL.n += 1
    _LOCAL.ms += ms
    if ms > _LOCAL.peor_ms:
        _LOCAL.peor_ms = ms
        _LOCAL.peor_sql = " ".join(str(sql).split())[:300]


def cerrar(ruta: str, metodo: str, ms: float, codigo: int = 200) -> None:
    """El request terminó: se guarda lo contado. La llama el `after_request`.

    `ruta` es la REGLA (`/facturas/<numf>`) y no la URL: si fuera la URL, cada
    factura sería una pantalla distinta y no se podría sumar nada.
    """
    activo = getattr(_LOCAL, "activo", False)
    n, ms_sql = getattr(_LOCAL, "n", 0), getattr(_LOCAL, "ms", 0.0)
    peor_ms, peor_sql = getattr(_LOCAL, "peor_ms", 0.0), getattr(_LOCAL, "peor_sql", "")
    _LOCAL.activo = False
    if not activo or not ruta:
        return

    with _LOCK:
        fila = _RUTAS.get(ruta)
        if fila is None:
            if len(_RUTAS) >= MAX_RUTAS:
                return
            fila = _RUTAS[ruta] = {
                "ruta": ruta, "metodo": metodo, "visitas": 0, "ms": [],
                "ms_max": 0.0, "consultas": 0, "consultas_max": 0,
                "ms_sql": 0.0, "peor_sql": "", "peor_sql_ms": 0.0,
            }
        fila["visitas"] += 1
        fila["ms"].append(ms)
        del fila["ms"][:-MUESTRAS_POR_RUTA]
        fila["ms_max"] = max(fila["ms_max"], ms)
        fila["consultas"] += n
        fila["consultas_max"] = max(fila["consultas_max"], n)
        fila["ms_sql"] += ms_sql
        if peor_ms > fila["peor_sql_ms"]:
            fila["peor_sql_ms"], fila["peor_sql"] = peor_ms, peor_sql

        if ms >= LENTA_MS:
            _LENTAS.append({
                "ruta": ruta, "metodo": metodo, "codigo": codigo, "ms": round(ms),
                "consultas": n, "ms_sql": round(ms_sql),
                "peor_sql": peor_sql, "peor_sql_ms": round(peor_ms),
                "cuando": time.time(),
            })
            del _LENTAS[:-MAX_LENTAS]


# ── lo que lee la pantalla ──────────────────────────────────────────────────


def resumen() -> list[dict]:
    """Una fila por pantalla, de la que MÁS TIEMPO SE LLEVA a la que menos.

    ⭐ El orden es por tiempo TOTAL (visitas × mediana) y no por la más lenta.
    Una pantalla de 3 segundos que se abre una vez por mes molesta menos que
    una de 400 ms que se abre doscientas veces por día, y la segunda es la que
    conviene arreglar. La más lenta se ve igual, en su columna.
    """
    with _LOCK:
        filas = [dict(f) for f in _RUTAS.values()]
    salida = []
    for f in filas:
        muestras = f.pop("ms") or [0.0]
        f["mediana_ms"] = round(statistics.median(muestras))
        f["ms_max"] = round(f["ms_max"])
        f["total_s"] = round(f["mediana_ms"] * f["visitas"] / 1000, 1)
        f["consultas_prom"] = round(f["consultas"] / max(1, f["visitas"]), 1)
        f["ms_sql_prom"] = round(f["ms_sql"] / max(1, f["visitas"]))
        f["peor_sql_ms"] = round(f["peor_sql_ms"])
        salida.append(f)
    salida.sort(key=lambda f: -f["total_s"])
    return salida


def lentas() -> list[dict]:
    """Las últimas visitas que pasaron de `LENTA_MS`, la más nueva primero."""
    with _LOCK:
        return list(reversed(_LENTAS))


def estado() -> dict:
    """Desde cuándo se está midiendo y cuánto se juntó."""
    with _LOCK:
        visitas = sum(f["visitas"] for f in _RUTAS.values())
        return {
            "desde": _DESDE,
            "minutos": round((time.time() - _DESDE) / 60),
            "pantallas": len(_RUTAS),
            "visitas": visitas,
            "lentas": len(_LENTAS),
        }


def limpiar() -> None:
    """Volver a empezar de cero (el botón de la pantalla, y los tests)."""
    global _DESDE
    with _LOCK:
        _RUTAS.clear()
        _LENTAS.clear()
        _DESDE = time.time()
