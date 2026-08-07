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

#: Rótulos cortos para la grilla. Con `table-layout:fixed` el ancho lo fija el
#: CSS, pero un encabezado largo igual obliga a envolver en dos líneas y empuja
#: la tabla fuera de la pantalla. En el detalle siguen con el nombre completo.
ETIQUETAS_CORTAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheq.",
    "facturas": "Fact.", "antic": "Antic.", "vsto": "Stock",
    "vqx": "Quím.", "totp": "Pasivos",
}

#: Las etapas del stock, en KILOS: es lo que guarda la foto (la tarifa está
#: sólo para el hilado, así que el valor de tejido y terminado no se puede
#: reconstruir). Acá sí va el nivel, porque un stock de tela es un número que
#: se mira, no un movimiento.
#: TMT 2026-08-06: *"hilado tejido y terminado puede aparecer en vez de
#: 1.939.121, 1.939 y ya"*. Son toneladas: 1.939.121 kg = 1.939 t. Siete
#: dígitos por columna para una cifra que se mira de reojo es ancho tirado.
#: TMT 2026-08-06: *"a hilado, tejido y terminado ponéles una k en el número
#: completo, así se diferencia de los cambios chiquitos del detalle"*. El nivel
#: va en miles con la "k" pegada (1.939k) y el Δ del detalle en kilos pelados
#: (−22): dos escalas en la misma columna, imposibles de confundir.
COLUMNAS_KG = (("hilado_kg", "Hilado"), ("tejido_kg", "Tejido"),
               ("terminado_kg", "Terminado"))

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
            fila["d_kg"] = {}
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
        # Los kilos que se movieron en cada etapa, para poder poner el − y el +
        # en su columna (TMT 2026-08-06: *"+ en una columna y − en la otra"*).
        # Salen de las dos fotos, así que no hay que guardarlos en el
        # movimiento.
        fila["d_kg"] = {}
        for col, _lab in COLUMNAS_KG:
            try:
                d = round(float(f.get(col) or 0) - float(prev.get(col) or 0), 2)
            except (TypeError, ValueError):
                continue
            if abs(d) >= 1:
                fila["d_kg"][col] = d
        out.append(fila)
    return out


def marcar_residuo(filas: list[dict]) -> list[dict]:
    """Le pone a cada foto si sus documentos explican el Δ o no.

    ⭐ TMT 2026-08-06: hasta acá había que abrir fila por fila para descubrir
    que una ventana no cerraba. La métrica del entrenamiento es el residuo, así
    que tiene que perseguirla a ella y no ella a él: la fila que no cierra se
    marca en el listado.

    Una consulta para todas las fotos, no una por fila. Las anteriores a la
    grabadora quedan con `residuo = None`: no es que no cierren, es que no hay
    con qué (`sin_registro`).
    """
    ids = [f.get("id_traza") for f in (filas or []) if f.get("id_traza")]
    if not ids:
        return filas or []
    try:
        agr = db.fetch_all(
            """
            SELECT id_traza,
                   COALESCE(SUM(aporte), 0)                              AS explicado,
                   COUNT(*) FILTER (WHERE familia = 'sin_explicar')      AS ciegos
              FROM scintela.dia_movimiento
             WHERE id_traza = ANY(%s)
             GROUP BY id_traza
            """, (ids,)) or []
    except Exception as e:  # noqa: BLE001 -- el listado tiene que salir igual
        _LOG.warning("traza_utilidad: no pude leer los residuos (%s)", e)
        return filas
    por_id = {int(r["id_traza"]): r for r in agr}
    primera = _desde_cuando_hay_detalle()
    for f in filas:
        idt = f.get("id_traza")
        r = por_id.get(int(idt)) if idt else None
        f["sin_registro"] = bool(primera is None or (idt and int(idt) < primera))
        f["ciegos"] = int((r or {}).get("ciegos") or 0)
        if f["sin_registro"] or f.get("d_utilidad") is None:
            f["residuo"] = None
            continue
        explicado = float((r or {}).get("explicado") or 0)
        f["residuo"] = round(float(f["d_utilidad"]) - explicado, 2)
    return filas


def sin_cerrar(filas: list[dict], umbral: float = 1.0) -> list[dict]:
    """Las ventanas cuyos documentos NO explican el Δ. La lista de tareas."""
    return [f for f in (filas or [])
            if f.get("residuo") is not None and abs(f["residuo"]) >= umbral]


def filtrar_por_componente(filas: list[dict], col: str | None) -> list[dict]:
    """Sólo las fotos en las que se movió ese componente.

    ⭐ Con 200 filas, poder ver únicamente las ventanas donde se movieron las
    facturas es la diferencia entre buscar y encontrar.
    """
    if not col or col not in dict(COMPONENTES):
        return filas or []
    return [f for f in (filas or []) if col in (f.get("movidas") or set())]


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


#: Debajo de esto un movimiento no se muestra: es redondeo, no una noticia.
#: TMT 2026-08-06, viendo un renglón que decía "-0,00 kg · Stock hilado −0,01":
#: *"no movió nada '0', ¿por qué lo mostrás?"*. Lo que se saca no se pierde —
#: se junta en un solo renglón "otros menores" si en conjunto llega a pesar.
UMBRAL_VISIBLE = 1.0

#: Cómo se dice en plural cada regla, para poder resumir. TMT 2026-08-06:
#: *"pedí detalle pero resumí… ejemplo: dos cheques por X monto, facturas por X
#: monto"*. Una ventana con doce documentos son doce renglones que nadie lee;
#: agrupados por lo que SON, son tres.
PLURALES = {
    "Venta facturada": "facturas nuevas",
    "Cheque recibido": "cheques recibidos",
    "Cheque depositado o dado de baja": "cheques depositados",
    "Cheque corregido": "cheques corregidos",
    "Abono a factura": "abonos a facturas",
    "Factura corregida en más": "facturas corregidas",
    "Factura cancelada del todo": "facturas canceladas",
    "Movimiento bancario": "movimientos bancarios",
    "Ingreso de caja": "ingresos de caja",
    "Gasto de caja": "gastos de caja",
    "Deuda nueva cargada": "deudas nuevas",
    "Deuda pagada o dada de baja": "deudas pagadas",
    "Deuda corregida": "deudas corregidas",
    "Anticipo entregado": "anticipos entregados",
    "Anticipo aplicado": "anticipos aplicados",
    "Retiro de dividendos": "retiros",
    "Sin explicar todavía": "sin explicar",
}


def _quien(etiqueta: str | None) -> str:
    """El cliente (o proveedor) que hay al final de la etiqueta, si lo hay.

    Las etiquetas de documento vienen como "Factura 001-099-000181251 · AJT" o
    "Cheque 0004512 · PGQ": el código va último. Un concepto largo
    —"Caja S · FLETE MERCADERIA QUITO"— no es un código y se descarta.
    """
    partes = [p.strip() for p in (etiqueta or "").split("·")]
    if len(partes) < 2:
        return ""
    q = partes[-1]
    return q if 0 < len(q) <= 8 else ""


def resumir(movs: list[dict], d_utilidad: float | None) -> list[dict]:
    """Los movimientos agrupados por lo que SON, no uno por documento.

    Tres facturas nuevas son un renglón que dice "3 facturas nuevas", no tres
    renglones con el número de cada una. Cuando el grupo es uno solo, se
    muestra el documento —ahí sí sirve saber cuál—.

    Lo que pesa menos de un peso no sale: se junta al final, y sólo si entre
    todos llegan a pesar. Si igual queda una diferencia contra el Δ, sale como
    "resto": la suma de lo que se ve tiene que dar el total, o la tabla deja de
    ser creíble.
    """
    grupos: dict[str, dict] = {}
    for m in movs or []:
        r = (m.get("regla") or "—").strip()
        g = grupos.setdefault(r, {"regla": r, "aporte": 0.0, "n": 0,
                                  "etiqueta": m.get("etiqueta"),
                                  "url": m.get("url"),
                                  "col": m.get("componente"),
                                  "quienes": {},
                                  "familia": m.get("familia")})
        if g["col"] != m.get("componente"):
            g["col"] = None                    # el grupo cruza componentes
        ap = float(m.get("aporte") or 0)
        g["aporte"] = round(g["aporte"] + ap, 2)
        g["n"] += 1
        q = _quien(m.get("etiqueta"))
        if q:
            g["quienes"][q] = round(g["quienes"].get(q, 0.0) + ap, 2)
    out, menores = [], 0.0
    for g in sorted(grupos.values(), key=lambda x: abs(x["aporte"]), reverse=True):
        if abs(g["aporte"]) < UMBRAL_VISIBLE:
            menores = round(menores + g["aporte"], 2)
            continue
        if g["n"] > 1:
            # ⭐ TMT 2026-08-06: *"decime algo de las facturas, de los abonos…
            # ¿clientes quizás?"*. "3 facturas nuevas" no dice nada; "3
            # facturas · AJT, SAC, GBC" dice de quién es la venta. Van los tres
            # que más pesan, que es lo que se mira.
            quienes = sorted(g["quienes"], key=lambda k: abs(g["quienes"][k]),
                             reverse=True)
            nombres = ", ".join(quienes[:3])
            if len(quienes) > 3:
                nombres += f" +{len(quienes) - 3}"
            g["texto"] = f"{g['n']} {PLURALES.get(g['regla'], g['regla'].lower())}"
            if nombres:
                g["texto"] += f" · {nombres}"
            g["url"] = None                    # son varios: ninguna ficha sola
        else:
            g["texto"] = g.get("etiqueta") or g["regla"]
        out.append(g)
    if abs(menores) >= UMBRAL_VISIBLE:
        out.append({"texto": "otros menores", "aporte": menores, "n": 0,
                    "url": None, "col": None, "familia": "utilidad"})
    if d_utilidad is not None:
        resto = round(d_utilidad - sum(g["aporte"] for g in out), 2)
        if abs(resto) >= UMBRAL_VISIBLE:
            out.append({"texto": "resto", "aporte": resto, "n": 0,
                        "url": None, "col": None, "familia": "sin_explicar"})
    return out


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

    fila["resumen"] = resumir(movs, fila.get("d_utilidad"))
    fila["d_kg"] = fila.get("d_kg") or {}

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
