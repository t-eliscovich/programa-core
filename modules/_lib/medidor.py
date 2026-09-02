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
#: Lo último que hizo el calentador (`modules/_lib/warmup.py`): ver abajo.
_CALENTADOR: dict = {}


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
    _LOCAL.puente_ms = 0.0
    _LOCAL.puente_n = 0
    _LOCAL.asinfo_ms = 0.0
    _LOCAL.formulas_ms = 0.0
    _LOCAL.peor_puente_ms = 0.0
    _LOCAL.peor_puente = ""


def anotar_puente(ms: float, fuente: str = "asinfo", detalle: str = "") -> None:
    """Una ida al PUENTE que terminó — Metabase (Asinfo) o la base de formulas.

    TMT 2026-09-02 (dueña: *"¿páginas lentas?"*). Medidas las pantallas en
    vivo, las lentas tenían casi nada de base: /produccion-terminado-asinfo
    tardó 10,3 s con 7 ms de consultas. El resto era el puente, y la pantalla
    no lo mostraba — se veía "de eso, base 7 ms" y había que adivinar el
    resto. Ahora el puente se cuenta aparte, con la misma regla que las
    consultas: en un hilo de fondo no cuenta nada.
    """
    if not getattr(_LOCAL, "activo", False):
        return
    _LOCAL.puente_ms = getattr(_LOCAL, "puente_ms", 0.0) + ms
    _LOCAL.puente_n = getattr(_LOCAL, "puente_n", 0) + 1
    # Y por separado, porque se arreglan distinto: Asinfo pasa por Metabase
    # (≥ 500 ms cada ida, se cachea) y formulas es un Postgres al lado (ms).
    if fuente == "formulas":
        _LOCAL.formulas_ms = getattr(_LOCAL, "formulas_ms", 0.0) + ms
    else:
        _LOCAL.asinfo_ms = getattr(_LOCAL, "asinfo_ms", 0.0) + ms
    # La ida más lenta, con qué era (el SQL de formulas o la base de Metabase):
    # es lo que dice QUÉ cachear. Texto de código, no datos.
    if ms > getattr(_LOCAL, "peor_puente_ms", 0.0):
        _LOCAL.peor_puente_ms = ms
        _LOCAL.peor_puente = f"{fuente}: " + " ".join(str(detalle).split())[:300]


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
    puente_ms, puente_n = getattr(_LOCAL, "puente_ms", 0.0), getattr(_LOCAL, "puente_n", 0)
    asinfo_ms = getattr(_LOCAL, "asinfo_ms", 0.0)
    formulas_ms = getattr(_LOCAL, "formulas_ms", 0.0)
    peor_puente_ms = getattr(_LOCAL, "peor_puente_ms", 0.0)
    peor_puente = getattr(_LOCAL, "peor_puente", "")
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
                "ms_puente": 0.0, "puente": 0, "ms_asinfo": 0.0, "ms_formulas": 0.0,
                "peor_puente": "", "peor_puente_ms": 0.0,
            }
        fila["visitas"] += 1
        fila["ms"].append(ms)
        del fila["ms"][:-MUESTRAS_POR_RUTA]
        fila["ms_max"] = max(fila["ms_max"], ms)
        fila["consultas"] += n
        fila["consultas_max"] = max(fila["consultas_max"], n)
        fila["ms_sql"] += ms_sql
        fila["ms_puente"] += puente_ms
        fila["puente"] += puente_n
        fila["ms_asinfo"] += asinfo_ms
        fila["ms_formulas"] += formulas_ms
        if peor_puente_ms > fila["peor_puente_ms"]:
            fila["peor_puente_ms"], fila["peor_puente"] = peor_puente_ms, peor_puente
        if peor_ms > fila["peor_sql_ms"]:
            fila["peor_sql_ms"], fila["peor_sql"] = peor_ms, peor_sql

        if ms >= LENTA_MS:
            _LENTAS.append({
                "ruta": ruta, "metodo": metodo, "codigo": codigo, "ms": round(ms),
                "consultas": n, "ms_sql": round(ms_sql),
                "puente": puente_n, "ms_puente": round(puente_ms),
                "ms_asinfo": round(asinfo_ms), "ms_formulas": round(formulas_ms),
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
        f["ms_puente_prom"] = round(f["ms_puente"] / max(1, f["visitas"]))
        f["puente_prom"] = round(f["puente"] / max(1, f["visitas"]), 1)
        f["ms_asinfo_prom"] = round(f["ms_asinfo"] / max(1, f["visitas"]))
        f["ms_formulas_prom"] = round(f["ms_formulas"] / max(1, f["visitas"]))
        f["peor_puente_ms"] = round(f["peor_puente_ms"])
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


# ── el calentador ───────────────────────────────────────────────────────────
# El calentador (`warmup.py`) refresca las cachés de Asinfo cada 60 s para que
# nadie pague la carga fría. Cuando una pantalla sale fría igual, la pregunta
# es "¿el calentador llegó a esa pantalla, y cuándo?" — y hasta hoy sólo lo
# decía el log del servidor Windows. Acá queda el último ciclo, en memoria.


def anotar_calentador(pasos: list[dict], duracion_s: float) -> None:
    """Terminó un ciclo del calentador: qué pasos corrió y cuánto tardó cada uno."""
    with _LOCK:
        _CALENTADOR.update({
            "ciclos": _CALENTADOR.get("ciclos", 0) + 1,
            "fin": time.time(),
            "duracion_s": round(duracion_s, 1),
            "pasos": [dict(p) for p in pasos],
        })


def calentador() -> dict:
    """El último ciclo del calentador, listo para la pantalla."""
    with _LOCK:
        c = dict(_CALENTADOR)
    if not c:
        return {}
    pasos = c.get("pasos") or []
    c["hace_s"] = round(time.time() - c["fin"])
    c["lentos"] = sorted(pasos, key=lambda p: -p["ms"])[:5]
    c["errores"] = [p for p in pasos if p.get("error")]
    c["n_pasos"] = len(pasos)
    return c


def limpiar() -> None:
    """Volver a empezar de cero (el botón de la pantalla, y los tests).

    ⚠ También apaga la medición A MEDIO HACER de este hilo. Sin eso, "volver a
    empezar" dejaba abierto un request que había llamado a `arrancar()` y nunca
    a `cerrar()` —pasa cuando la respuesta no sale por el `after_request`: un
    404, un handler de error—, y el PRÓXIMO `cerrar()` de ese hilo se anotaba
    como si fuera una visita.

    Lo cazó el CI el 26/08/2026: `test_una_consulta_de_un_hilo_de_fondo_no_se_cuenta`
    falló en el servidor y pasaba local. La fixture limpiaba las dos tablas pero
    no el thread-local, así que el `activo=True` que había dejado otro test
    sobrevivía a la limpieza y el `cerrar()` del test se contaba. Un test que
    depende de lo que corrió antes deja el CI en rojo y frena el deploy.
    """
    global _DESDE
    _LOCAL.activo = False
    _LOCAL.n = 0
    _LOCAL.ms = 0.0
    _LOCAL.peor_ms = 0.0
    _LOCAL.peor_sql = ""
    with _LOCK:
        _RUTAS.clear()
        _LENTAS.clear()
        _CALENTADOR.clear()
        _DESDE = time.time()
