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


def registrar(origen: str = "manual", bal: dict | None = None,
              momento: str = "foto") -> dict:
    """Guarda UNA foto: los totales, el detalle por documento, y el diff.

    Hasta la mig 0171 esto guardaba sólo los once totales, así que la pantalla
    podía decir *"Facturas +11.399"* y ahí se terminaba. Ahora, en la misma
    vuelta y contra el MISMO balance, saca la foto a nivel documento
    (`foto.detalle`), la compara con la de hace cinco minutos y guarda qué se
    movió. Esa es la diferencia entre *"se movieron las facturas"* y *"la
    factura 001-099-000181251 de AJT, $ 10.741,46"*.

    Devuelve un dict (antes devolvía un bool) porque el ancla del día necesita
    el `id_traza` y el balance ya calculado: `dia.capturar()` cuelga de esta
    misma foto en vez de leer el balance una segunda vez.

    Nunca lanza: la grabadora no puede tumbar nada.
    """
    res: dict = {"ok": False, "id_traza": None, "movimientos": 0,
                 "primera": False, "motivo": "", "bal": bal}
    try:
        if bal is None:
            from modules.informes import queries as _q
            bal = _q.informe_balance()
        res["bal"] = bal
        fila = _fila_desde_balance(bal)
        if fila.get("utilidad") is None:
            res["motivo"] = "balance sin componentes"
            return res                        # balance sin componentes: no sirve
        fila["origen"] = (origen or "manual")[:20]
        fila["momento"] = (momento or "foto")[:20]

        from modules.informes import foto as _foto
        nueva = _foto.detalle(bal)
        vieja = _foto.guardada()
        primera = _foto.es_primera(vieja)
        movs = [] if primera else _foto.diff(nueva, vieja)

        cols = list(fila.keys())
        campos = ", ".join(cols)
        marcas = ", ".join(f"%({c})s" for c in cols)
        # Una sola transacción: la foto, sus movimientos y el estado nuevo del
        # detalle entran juntos o no entra nada. Si el detalle avanzara sin que
        # entren los movimientos, esos documentos quedarían sin explicación
        # para siempre — la vuelta siguiente ya los vería como "iguales".
        with db.tx() as conn:
            r = db.execute_returning(
                f"INSERT INTO scintela.traza_utilidad ({campos}) "
                f"VALUES ({marcas}) RETURNING id_traza", fila, conn=conn)
            idt = r["id_traza"]
            _foto.guardar_movimientos(conn, idt, movs)
            _foto.aplicar(conn, nueva, vieja)
        res.update(ok=True, id_traza=idt, movimientos=len(movs), primera=primera)
    except Exception as e:  # noqa: BLE001 -- la grabadora no puede tumbar nada
        _LOG.warning("traza_utilidad: no se pudo guardar la foto (%s)", e)
        res["motivo"] = str(e)[:150]
    return res


def registrar_si_toca(origen: str = "loop") -> bool:
    """Llamada desde el ciclo de fondo: guarda si pasó el intervalo."""
    global _ultimo_ts
    ahora = time.time()
    if (ahora - _ultimo_ts) < _intervalo():
        return False
    _ultimo_ts = ahora
    return bool(registrar(origen=origen).get("ok"))


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


#: Las columnas de la grilla. Muestran el Δ del componente, no su saldo.
#:
#: TMT 2026-08-06, en dos pasos. Primero *"pongamos más columnas, ahora que hay
#: detalle ya es inútil el que se movió"*; se probó con los SALDOS y la
#: respuesta fue *"el formato no me gustó pero claramente necesitamos más
#: columnas"*. El problema del saldo es que son catorce cifras de siete dígitos
#: casi idénticas fila contra fila: para encontrar el movimiento hay que restar
#: a ojo. El Δ ya ES la respuesta, y la celda queda vacía cuando no pasó nada,
#: así que el ojo cae solo sobre lo que se movió.
#:
#: Los saldos no se pierden: están en el Balance, y en el detalle de cada foto.
COLUMNAS_DELTA = ("caja", "bancos", "cheques", "facturas", "antic",
                  "vsto", "vqx", "totp")

#: Las etapas del stock, en KILOS: es lo que guarda la foto (la tarifa está
#: sólo para el hilado, así que el valor de tejido y terminado no se puede
#: reconstruir). Acá sí va el nivel, porque un stock de tela es un número que
#: se mira, no un movimiento.
COLUMNAS_KG = (("hilado_kg", "Hilado kg"), ("tejido_kg", "Tejido kg"),
               ("terminado_kg", "Terminado kg"))

#: Lo que la grilla vigila para saber si un kilo o la tarifa se movieron. El
#: $/kg entra con umbral propio: se mueve en la cuarta decimal y un salto de
#: milésimas revalúa el stock entero.
_VIGILADAS = tuple(c for c, _l in COLUMNAS_KG) + ("hilado_ukg",)


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
            fila["movidas"] = set()
            fila["delta"] = {}
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
        # ⭐ Cuáles se movieron, para que la grilla de SALDOS pueda apagar los
        # que quedaron iguales. Una columna de saldos es ilegible si los siete
        # dígitos que no cambiaron pesan lo mismo que el que sí: la dueña
        # tendría que restar a ojo, fila contra fila, para encontrar el
        # movimiento. Con los repetidos en gris claro, el que cambió salta.
        movidas = {m["col"] for m in movs}
        for col in _VIGILADAS:
            umbral = 0.00005 if col.endswith("_ukg") else 1.0
            try:
                if abs(float(f.get(col) or 0) - float(prev.get(col) or 0)) >= umbral:
                    movidas.add(col)
            except (TypeError, ValueError):
                continue
        fila["movidas"] = movidas
        # El Δ de cada componente, indexado por columna: la grilla muestra
        # esto y deja la celda vacía cuando no hay nada.
        fila["delta"] = {m["col"]: m["aporte"] for m in movs}
        out.append(fila)
    return out


def bajadas(filas: list[dict], umbral: float = 1000.0) -> list[dict]:
    """Las fotos en las que la utilidad BAJÓ más que `umbral`.

    Dueña: *"no podemos tener utilidad menor a la mañana"*. Esto es la lista de
    veces que pasó, con el componente que lo explica.
    """
    return [f for f in (filas or [])
            if f.get("d_utilidad") is not None and f["d_utilidad"] <= -abs(umbral)]


# ── El detalle de UNA foto: qué documentos movieron ese Δ ───────────────────

#: Prefijo del `doc_id` → tabla de origen, para reusar `historial.link_origen`
#: y no inventar una segunda tabla de links.
#: 🚨 Los links de este repo son strings hardcodeados, no `url_for`: una ruta
#: que no existe no se ve desde el código, sólo como 404 al clickear. Por eso
#: se delega en la función que YA tiene un test recorriendo todo el mapeo
#: contra el `url_map` real (`tests/test_historial_links_resuelven.py`).
PREFIJO_TABLA = {
    "f": "factura", "c": "cheque", "k": "caja", "d": "dolares",
    "p": "posdat", "r": "retiros",
}


def _ref(doc_id: str) -> tuple[str | None, int | None]:
    """(tabla, id interno) de un doc_id, o (None, None) si es sintético."""
    doc_id = (doc_id or "").split(":")[0]
    if not doc_id or doc_id.startswith("#"):
        return None, None
    tabla = PREFIJO_TABLA.get(doc_id[0])
    try:
        return (tabla, int(doc_id[1:])) if tabla else (None, None)
    except (TypeError, ValueError):
        return None, None


def _numeros_visibles(movs: list[dict]) -> tuple[dict, dict]:
    """id interno → número que la dueña reconoce, en dos queries.

    ⭐ La URL va por número visible (`numf`, `no_cheque`) y no por id interno,
    igual que en el historial: la dueña nombra las cosas por su número.
    """
    ids: dict[str, set] = {"factura": set(), "cheque": set()}
    for m in movs or []:
        tabla, rid = _ref(m.get("doc_id"))
        if tabla in ids and rid:
            ids[tabla].add(rid)
    numfs, nos = {}, {}
    if ids["factura"]:
        for r in db.fetch_all(
                "SELECT id_factura, numf FROM scintela.factura "
                "WHERE id_factura = ANY(%s)", (list(ids["factura"]),)) or []:
            numfs[int(r["id_factura"])] = r.get("numf")
    if ids["cheque"]:
        for r in db.fetch_all(
                "SELECT id_cheque, no_cheque FROM scintela.cheque "
                "WHERE id_cheque = ANY(%s)", (list(ids["cheque"]),)) or []:
            nos[int(r["id_cheque"])] = r.get("no_cheque")
    return numfs, nos


def movimientos(id_traza: int) -> list[dict]:
    """Los documentos que movieron esa foto, con su link a la ficha."""
    try:
        movs = db.fetch_all(
            "SELECT * FROM scintela.dia_movimiento "
            " WHERE id_traza = %s ORDER BY ABS(aporte) DESC", (int(id_traza),)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no se pudieron leer los movimientos (%s)", e)
        return []
    try:
        from modules.historial.queries import link_origen
        numfs, nos = _numeros_visibles(movs)
    except Exception as e:  # noqa: BLE001 -- sin links la pantalla igual sirve
        _LOG.warning("traza_utilidad: sin links (%s)", e)
        return [dict(m, url=None) for m in movs]
    out = []
    for m in movs:
        tabla, rid = _ref(m.get("doc_id"))
        url = None
        if tabla and rid:
            url, _rot = link_origen({"origen_table": tabla, "origen_id": rid},
                                    factura_numfs=numfs, cheque_nos=nos)
        out.append(dict(m, url=url))
    return out


def _desde_cuando_hay_detalle() -> int | None:
    """La primera foto que dejó movimientos, o None si todavía no hay ninguna.

    Las fotos anteriores a la mig 0171 guardaron los once totales y nada más:
    el detalle por documento no se puede reconstruir para atrás —aplicar un
    cheque ni siquiera sella `fecha_modifica` en la factura— así que para esas
    ventanas lo honesto es mostrar el desglose por COMPONENTE, que sí existe, y
    decir desde cuándo empieza el detalle fino.
    """
    try:
        r = db.fetch_one("SELECT MIN(id_traza) AS m FROM scintela.dia_movimiento "
                         "WHERE id_traza IS NOT NULL")
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no pude ver desde cuándo hay detalle (%s)", e)
        return None
    return int(r["m"]) if r and r.get("m") else None


def una(id_traza: int) -> dict | None:
    """UNA foto con su Δ contra la anterior y el detalle que lo explica.

    El residuo es la parte del Δ que los documentos no llegaron a explicar. A
    esta cadencia debería ser cero: si no lo es, la ventana de cinco minutos
    en la que se rompió el invariante queda señalada con nombre y hora.
    """
    try:
        id_traza = int(id_traza)
    except (TypeError, ValueError):
        return None
    try:
        par = db.fetch_all(
            """
            SELECT *,
                   TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil',
                           'DD/MM HH24:MI') AS cuando
              FROM scintela.traza_utilidad
             WHERE id_traza <= %s
             ORDER BY creado_en DESC, id_traza DESC
             LIMIT 2
            """, (id_traza,)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no se pudo leer la foto (%s)", e)
        return None
    if not par or int(par[0].get("id_traza") or 0) != id_traza:
        return None
    fila = con_deltas(par)[0]
    fila["anterior"] = par[1] if len(par) > 1 else None
    movs = movimientos(id_traza)
    fila["movimientos"] = movs

    # Por componente, para poder poner los documentos abajo del renglón que ya
    # muestra la tabla de arriba.
    por_comp: dict[str, dict] = {}
    for m in movs:
        c = m.get("componente") or "?"
        g = por_comp.setdefault(c, {"col": c, "label": ETIQUETAS.get(c, c),
                                    "aporte": 0.0, "movimientos": []})
        g["aporte"] = round(g["aporte"] + float(m.get("aporte") or 0), 2)
        g["movimientos"].append(m)
    fila["por_componente"] = sorted(
        por_comp.values(), key=lambda g: abs(g["aporte"]), reverse=True)

    # 🚨 "Sin movimientos" y "sin registro" NO son lo mismo. Una ventana en la
    # que de verdad no se movió nada y una anterior a que existiera la
    # grabadora de detalle se ven idénticas —las dos con la lista vacía— y
    # decirle a la dueña "no se movió ningún documento" sobre una ventana de
    # 2.000 dólares es mentirle.
    desde = _desde_cuando_hay_detalle()
    fila["sin_registro"] = bool(not movs and (desde is None or id_traza < desde))

    total = round(sum(float(m.get("aporte") or 0) for m in movs), 2)
    fila["explicado"] = total
    fila["residuo"] = (round(float(fila.get("d_utilidad") or 0) - total, 2)
                       if fila.get("d_utilidad") is not None else None)
    fila["ciegos"] = [m for m in movs if m.get("familia") == "sin_explicar"]
    return fila
