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
import re
import time

import db

_LOG = logging.getLogger("programa_core.traza_utilidad")

#: Cada cuánto se guarda una foto. 5 minutos = el TTL de las cachés de Asinfo,
#: así que cada foto ve datos nuevos y no repetimos filas idénticas.
INTERVALO_SECS = 300

_ultimo_ts: float = 0.0

#: Clave del advisory lock que serializa las fotos. Un número cualquiera, pero
#: fijo: dos procesos tienen que pedir el MISMO.
LOCK_FOTO = 817_2026


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
        vieja = _foto.guardada()
        nueva = _foto.detalle(bal, anterior=vieja)
        # 🚨 El balance y el detalle son DOS lecturas distintas de una base que
        # se está usando: si entre una y otra alguien aplica un cheque, el
        # componente no cierra contra sus documentos y sale un renglón "sin
        # explicar" que a los tres minutos aparece con el signo dado vuelta
        # (medido el 07/08: −289 a las 13:33, +289 a las 13:36). No es plata
        # ciega, es el reloj. Se rehace UNA vez: con la app quieta las dos
        # lecturas coinciden y el renglón no llega a nacer. Si al segundo
        # intento sigue sin cerrar, entonces sí hay algo que mirar y se guarda
        # como está — para eso está el renglón.
        if _foto.hay_desfase(nueva):
            from modules.informes import queries as _q2
            bal = _q2.informe_balance()
            res["bal"] = bal
            fila2 = _fila_desde_balance(bal)
            if fila2.get("utilidad") is not None:
                fila2["origen"] = fila["origen"]
                fila2["momento"] = fila["momento"]
                fila = fila2
                nueva = _foto.detalle(bal, anterior=vieja)
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
            # 🚨 `nueva` y `vieja` se leen fuera de la transacción y
            # `dia_movimiento` no tiene unicidad: dos vueltas simultáneas —el
            # ciclo de fondo y el botón "Sacar foto ahora"— producen dos fotos
            # con LOS MISMOS movimientos, y la explicación del día los suma dos
            # veces. El lock es por sesión y se suelta al cerrar la
            # transacción; si otro lo tiene, esta vuelta se saltea.
            tomado = db.fetch_one(
                "SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_FOTO,),
                conn=conn)
            if not (tomado or {}).get("ok"):
                res["motivo"] = "otra foto se está sacando ahora"
                return res
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

#: Los tres que la dueña sacó de la grilla ("dividendos, maquinaria, terrenos
#: no necesito"). Siguen moviendo la utilidad —la amortización, todos los
#: días— así que su suma va en una columna "Otros": sin ella, esas filas
#: muestran un Δ con las ocho celdas vacías, que es el síntoma de "no cierra".
COLUMNAS_OTROS = ("umaq", "uact", "uret")

#: Rótulos cortos para la grilla. Con `table-layout:fixed` el ancho lo fija el
#: CSS, pero un encabezado largo igual obliga a envolver en dos líneas y empuja
#: la tabla fuera de la pantalla. En el detalle siguen con el nombre completo.
ETIQUETAS_CORTAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheq.",
    "facturas": "Fact.", "antic": "Antic.", "vsto": "Stock",
    "vqx": "Quím.", "totp": "Pasivos",
}

#: Los MISMOS rótulos, recortados para el encabezado de la grilla. 🚨 TMT
#: 2026-08-07: *"achicá las columnas, abreviá si es necesario, sí o sí tiene
#: que entrar en la pantalla sin scrollear"*. Con dieciséis columnas el ancho
#: lo fijan los TÍTULOS, no los números: "Terminado" pide 110 px para mostrar
#: "-549". Medido en el navegador: 1.325 px de tabla contra 1.241 de caja.
#: El nombre largo sigue en el `title` de cada columna, y `ETIQUETAS_CORTAS`
#: —que también arma los renglones "Bancos: sin explicar por documento"— no se
#: toca: ahí el ancho sobra.
ETIQUETAS_GRILLA = {
    "caja": "Caja", "bancos": "Bco.", "cheques": "Chq.",
    "facturas": "Fac.", "antic": "Ant.", "vsto": "Stk.",
    "vqx": "Quí.", "totp": "Pas.",
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
COLUMNAS_KG = (("hilado_kg", "Hil."), ("tejido_kg", "Tej."),
               ("terminado_kg", "Term."))

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
             ORDER BY creado_en DESC, id_traza DESC
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
        otros = round(sum(m["aporte"] for m in movs
                          if m["col"] in COLUMNAS_OTROS), 2)
        if abs(otros) >= 1:
            fila["delta"]["otros"] = otros
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
        # 🚨 Los reconstruidos NO cuentan: si contaran, `sin_registro` se
        # apagaría para las 200 fotos viejas y el aviso de "ventanas que no
        # cierran" las tomaría como problemas. Serían 200 tareas inventadas
        # tapando las de verdad.
        r = db.fetch_one("SELECT MIN(id_traza) AS m FROM scintela.dia_movimiento "
                         "WHERE id_traza IS NOT NULL AND tipo <> 'reconstruido'")
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
    # Las que faltaban: salían como "20 movimiento de caja", "37 amortización
    # del día", "2 stock". Origen: `foto.regla()` y la reconstrucción.
    "Movimiento de caja": "movimientos de caja",
    "Stock": "movimientos de stock",
    "Revaluación de stock": "revaluaciones de stock",
    "Stock de químicos": "movimientos de químicos",
    "Amortización del día": "activos amortizados",
    "Activo dado de alta": "activos dados de alta",
    "Activo dado de baja": "activos dados de baja",
    "Cheques emitidos sin debitar": "cheques emitidos sin debitar",
    "Apertura de caja": "aperturas de caja",
}


#: Un código de cliente o proveedor: 2 a 4 caracteres, mayúsculas o dígitos.
_RE_CODIGO = re.compile(r"^[A-Z0-9]{2,4}$")


def _quien(etiqueta: str | None, comp: str | None = None) -> str:
    """El cliente (o proveedor) que hay al final de la etiqueta, si lo hay.

    Las etiquetas de documento vienen como "Factura 001-099-000181251 · AJT" o
    "Cheque 0004512 · PGQ": el código va último. Un concepto largo
    —"Caja S · FLETE MERCADERIA QUITO"— no es un código y se descarta.
    """
    partes = [p.strip() for p in (etiqueta or "").split("·")]
    if len(partes) < 2:
        return ""
    # 🚨 En facturas y cheques la contraparte va ÚLTIMA ("Factura 123 · AJT"),
    # pero en anticipos, deudas y retiros el último segmento es el CONCEPTO
    # ("Anticipo AI · 21" daba "21", que no significa nada). Ahí la contraparte
    # es el segundo token del primer segmento.
    if comp in ("antic", "totp", "uret"):
        cabeza = partes[0].split()
        q = cabeza[1] if len(cabeza) > 1 else ""
    elif comp in ("caja", "bancos"):
        return ""                          # un concepto no es una contraparte
    else:
        q = partes[-1]
    # 🚨 Un concepto corto no es un código. "Caja S · LUZ" daba "LUZ" y el
    # resumen terminaba diciendo "4 gastos de caja · LUZ, AGUA, TAXI". Los
    # códigos de cliente y de proveedor son 2 a 4 caracteres en mayúscula o
    # dígitos, y sólo los llevan los componentes que tienen contraparte.
    return q if _RE_CODIGO.match(q) else ""


#: 🚨 El texto del renglón de stock se escribe cuando se SACA la foto
#: (`foto._texto_stock`), no cuando se muestra: abreviarlo allá dejaba las 200
#: fotos que ya están guardadas con "hilado → tejido y terminado" — justo las
#: que la dueña mira. Se abrevia también al RENDERIZAR, que es donde se ve.
_RE_ETAPAS = re.compile(r"\b(hilado|tejido|terminado)\b")
_ABREV = {"hilado": "hil.", "tejido": "tej.", "terminado": "term."}


def _abreviar_etapas(texto: str) -> str:
    return _RE_ETAPAS.sub(lambda m: _ABREV[m.group(1)], texto or "")


#: Los tipos que llegan de a montones y se muestran en UN renglón con la
#: cuenta y los clientes. Medido sobre 200 ventanas: `factura_emitida` es el
#: renglón más repetido (51 de ~300) y `cheque_aplicado_a_factura` el que arma
#: las ventanas largas. Los demás tipos siguen con un renglón por hecho: una
#: nota de débito o una compra pasan de a una y ahí el detalle SÍ importa.
#: tipo → (rótulo, unidad). Con unidad, el renglón cuenta en esa unidad y no
#: nombra clientes: *"retenciones revertidas · 8 facturas"*. Sin unidad, cuenta
#: hechos y nombra a los tres que más pesan: *"4 FA · JVL, AJT, NIF +1"*.
#: Hechos que NETEAN CERO POR DEFINICIÓN y aun así son noticia. El totalizar
#: redistribuye los abonos de un cliente entre sus facturas sin crear ni borrar
#: deuda: su aporte al Δ es siempre 0,00 y sus documentos están todos en el
#: mismo componente, así que el `bruto` (el lado más grande de UN componente)
#: también da 0 y el renglón se caía entero a "movimientos chicos". Un
#: movimiento que tocó trece facturas no es un movimiento chico.
#: TMT 2026-08-07.
TIPOS_SIEMPRE_VISIBLES = {"totalizar_estado_cuenta",
                          "reverso_totalizar_estado_cuenta"}

TIPOS_QUE_SE_JUNTAN = {
    "factura_emitida": ("FA", ""),
    "cheque_aplicado_a_factura": ("CH → FA", ""),
    # Segunda pasada, 07/08: las devoluciones caían de a una (10 renglones en
    # el día, cuatro de ellas en la MISMA ventana que las 13 facturas nuevas)
    # y las compras de una importación también (tres "CP AQ → DE 1013x"
    # seguidas). Van aparte de las facturas nuevas: el signo es al revés y
    # mezclarlas escondería una devolución grande adentro de un neto.
    "factura_devolucion": ("FA dev", ""),
    "compra_a_posdat": ("CP → DE", ""),
    # 🚨 TMT 2026-08-07, viendo ocho renglones seguidos de "RT TNZ ✗ FA":
    # *"esto también juntalo, retenciones es un solo movimiento"*. La
    # DESaplicada no lleva `batch_id` —la aplicada sí, por eso ésa ya salía en
    # un renglón— así que caía de a una.
    "retencion_asinfo_desaplicada": ("retenciones revertidas", "facturas"),
    "reverso_retencion_asinfo_aplicada": ("retenciones revertidas", "facturas"),
    # La corrección de la mig 0179 (la retención sale del abono) también llega
    # de a montones y por el mismo motivo: se hace factura por factura.
    "retencion_movida_del_abono": ("retenciones fuera del abono", "facturas"),
}


#: Un documento SIN hecho con nombre: la etiqueta que guardó la foto. Sale
#: como "Factura 174039 · MLZ" al lado de renglones que dicen "FA #181305 JVL"
#: — dos formas de escribir lo mismo, y la larga se come el ancho de la
#: columna. TMT 2026-08-07: *"dale con todo"*.
_RE_ETIQUETA_DOC = re.compile(r"^(Factura|Cheque)\s+(\S+)\s+·\s+(.+)$")
_OBJETO = {"Factura": "FA", "Cheque": "CH"}


def _corto_etiqueta(etiqueta: str) -> str:
    m = _RE_ETIQUETA_DOC.match((etiqueta or "").strip())
    if not m:
        return etiqueta
    return f"{_OBJETO[m.group(1)]} #{m.group(2)} {m.group(3)}"


def resumir(movs: list[dict], d_utilidad: float | None,
            eventos: dict | None = None) -> list[dict]:
    """Los movimientos agrupados por lo que SON, no uno por documento.

    Tres facturas nuevas son un renglón que dice "3 facturas nuevas", no tres
    renglones con el número de cada una. Cuando el grupo es uno solo, se
    muestra el documento —ahí sí sirve saber cuál—.

    Lo que pesa menos de un peso no sale: se junta al final, y sólo si entre
    todos llegan a pesar. Si igual queda una diferencia contra el Δ, sale como
    "resto": la suma de lo que se ve tiene que dar el total, o la tabla deja de
    ser creíble.
    """
    grupos: dict[tuple, dict] = {}
    for m in movs or []:
        r = (m.get("regla") or "—").strip()
        # 🚨 Los once `#ajuste:<comp>` comparten la regla "Sin explicar
        # todavía". Agrupados sólo por regla, un +5.000 en caja y un −5.000 en
        # bancos se netean a cero: diez mil dólares de plata ciega no se
        # muestran, y como el neto da cero tampoco dispara el aviso. Justo al
        # revés de lo que ese aviso tiene que hacer. Lo ciego se agrupa por
        # COMPONENTE.
        # ⭐ Si `mov_doble` conoce el documento, el hecho manda sobre la regla:
        # el cheque que se va de cartera y la plata que aparece en el banco son
        # UN depósito, no dos cosas sueltas. Y un batch agrupa el hecho entero
        # ("3 anticipos → compra N° 10130").
        ev = (eventos or {}).get(m.get("doc_id") or "")
        if ev:
            # 🚨 TMT 2026-08-07: *"idealmente facturas en uno, cobranzas en
            # otro"*. Hay tipos que llegan de a montones —en la ventana de las
            # 13:33 había cinco facturas nuevas y tres cheques aplicados, ocho
            # renglones de doce— y uno por hecho es la lista de documentos otra
            # vez, que es justo lo que la pantalla no quiere ser. Esos se
            # juntan por TIPO; el resto sigue siendo un renglón por hecho.
            clave = (("tipo", ev["tipo"]) if ev["tipo"] in TIPOS_QUE_SE_JUNTAN
                     else ("ev", ev["grupo"]))
        elif m.get("familia") == "sin_explicar":
            clave = (r, m.get("componente"))
        else:
            clave = (r, None)
        g = grupos.setdefault(clave, {"regla": r, "aporte": 0.0, "n": 0,
                                  "etiqueta": m.get("etiqueta"),
                                  "url": m.get("url"),
                                  "col": m.get("componente"),
                                  "por_col": {},
                                  "quienes": {},
                                  "evento": ev,
                                  "hechos": set(),
                                  "familia": m.get("familia")})
        # 🚨 Cuántos HECHOS hay en el grupo, no cuántos documentos. Un cheque
        # aplicado a una factura toca dos documentos y es UN hecho: salía
        # "CH GSS → FA (2)", que se lee como dos cheques. Un depósito toca
        # ocho cheques y una cuenta y son ocho hechos, uno por cheque.
        if ev and ev.get("id_mov_doble"):
            g["hechos"].add(ev["id_mov_doble"])
        if g["col"] != m.get("componente"):
            g["col"] = None                    # el grupo cruza componentes
        ap = float(m.get("aporte") or 0)
        g["aporte"] = round(g["aporte"] + ap, 2)
        # 🚨 TMT 2026-08-07, sobre el depósito de $41.729 que la pantalla no
        # nombraba: *"siempre mostrar el cambio más grande primero — ¿qué es
        # más importante acá, los 40k que movieron o hilado y terminado?"*.
        # Un traspaso APORTA cero a la utilidad pero MUEVE lo más grande del
        # día; con un solo total el renglón valía 0 y se caía por chico. Se
        # guarda el movimiento de cada componente por separado: la fila dice
        # −41.729 en Cheq. y +41.729 en Bancos, y su aporte al Δ sigue siendo
        # cero, que es la verdad.
        c = m.get("componente")
        if c:
            g["por_col"][c] = round(g["por_col"].get(c, 0.0) + ap, 2)
        g["n"] += 1
        q = _quien(m.get("etiqueta"), m.get("componente"))
        if q:
            g["quienes"][q] = round(g["quienes"].get(q, 0.0) + ap, 2)
    for g in grupos.values():
        # Lo que el grupo MOVIÓ, aunque no haya aportado: el lado más grande.
        g["bruto"] = max((abs(v) for v in g["por_col"].values()), default=0.0)
    out, menores = [], 0.0
    for g in sorted(grupos.values(),
                    key=lambda x: max(abs(x["aporte"]), x["bruto"]),
                    reverse=True):
        ev = g.get("evento")
        # Lo ciego no se esconde por chico: es la lista de tareas. Y un
        # traspaso grande tampoco: aporta cero, pero es la noticia.
        if (abs(g["aporte"]) < UMBRAL_VISIBLE
                and g["bruto"] < UMBRAL_VISIBLE
                and g.get("familia") != "sin_explicar"
                and (ev or {}).get("tipo") not in TIPOS_SIEMPRE_VISIBLES):
            menores = round(menores + g["aporte"], 2)
            continue
        if ev:
            from modules.historial.queries import corto as _corto

            g["regla"] = ev["label"]
            quienes = sorted(g["quienes"], key=lambda k: abs(g["quienes"][k]),
                             reverse=True)
            md = ev.get("meta") or {}
            cerrado = False        # el texto ya cuenta cuántos son
            # La contraparte que sabe el evento manda sobre la que se adivina
            # de la etiqueta.
            quien = (md.get("codigo_prov") or (quienes[0] if quienes else ""))
            if ev["tipo"] in TIPOS_QUE_SE_JUNTAN and len(g["hechos"]) > 1:
                rotulo, unidad = TIPOS_QUE_SE_JUNTAN[ev["tipo"]]
                if unidad:
                    g["texto"] = f"{rotulo} · {len(g['hechos'])} {unidad}"
                else:
                    nombres = ", ".join(quienes[:3])
                    if len(quienes) > 3:
                        nombres += f" +{len(quienes) - 3}"
                    g["texto"] = (f"{len(g['hechos'])} {rotulo}"
                                  + (f" · {nombres}" if nombres else ""))
                cerrado = True
            elif ev.get("texto"):
                # El evento ya trae su renglón escrito (la cuenta bancaria con
                # varios hechos): no hay tipo que traducir.
                g["texto"] = ev["texto"]
            elif ev["tipo"] == "retencion_asinfo_aplicada":
                # ⭐ TMT 2026-08-07: *"sólo quiero saber que se aplicaron x
                # monto total a x facturas por retenciones"*. El cliente no
                # aporta acá: lo que explica el cambio de utilidad es cuánto
                # se retuvo y sobre cuántas facturas.
                g["texto"] = "retenciones"
            elif ev["tipo"] in ("factura_emitida", "factura_devolucion"):
                # 🚨 TMT 2026-08-07: la factura es lo que más sale en la traza
                # (7.451 hechos, el tipo N° 1) y salía como "FA KLC nueva" —
                # el cliente sí, pero NINGÚN número. Con cinco facturas en la
                # misma ventana no hay forma de saber cuál es cuál ni de
                # buscarla en Asinfo. El número va primero porque es lo que se
                # busca; el importe ya está en la columna Fact., así que no se
                # repite.
                numf = str(md.get("numf") or "").strip()
                g["texto"] = " ".join(
                    x for x in ("FA", f"#{numf}" if numf else "",
                                md.get("codigo_cli") or quien,
                                "dev" if ev["tipo"] == "factura_devolucion" else "")
                    if x)
            elif ev["tipo"].endswith("cheque_depositado") and md.get("n_grupo"):
                # 🚨 Un depósito consolidado escribe UNA `mov_doble` por cheque:
                # ocho cheques al banco salían como ocho renglones de una sola
                # ida al banco. Ahora es uno — y el código del PRIMER cheque no
                # va, porque son ocho clientes distintos. El paréntesis cuenta
                # CHEQUES (`n_grupo`), no documentos: en el grupo también está
                # el renglón de la cuenta bancaria.
                g["texto"] = f"{_corto(ev['tipo'])} ({int(md['n_grupo'])})"
                cerrado = True
            elif ev["tipo"].startswith("banco_") or ev["tipo"] == "nota_debito":
                # ⭐ Del lado del banco el NOMBRE del tipo no dice nada ("BC
                # movimiento", "BC nota de débito"): lo que la dueña reconoce
                # es el concepto que se tipeó en la pantalla de Bancos. El
                # nombre largo del tipo sigue en el `title`.
                g["texto"] = ((ev.get("concepto") or "").strip()
                              or _corto(ev["tipo"], quien))
            else:
                g["texto"] = _corto(ev["tipo"], quien)
            # El destino, cuando el evento lo sabe: "AN AI → CP 10130" dice a
            # qué compra fueron, que es lo que /historial muestra y acá faltaba.
            # …salvo en un renglón juntado: el número de UNA de las compras al
            # lado de "3 CP → DE" se lee como si fueran todas esa.
            # Un hecho que abarca N facturas lo dice: "FA MLZ totalizar · 12
            # facturas" en vez de dejar que se adivine por el importe.
            if md.get("n_facturas") and not cerrado:
                g["texto"] += f" · {int(md['n_facturas'])} facturas"
                cerrado = True
            if md.get("numero_compra") and not cerrado:
                g["texto"] += f" {md['numero_compra']}"
            # Cuando el hecho ES uno solo pero abarca varios documentos, el
            # número lo dice la metadata (`3 anticipos → compra 10130`); si no,
            # se cuentan los hechos.
            hechos = int(md.get("n_anticipos") or md.get("n_grupo") or 0) \
                or len(g["hechos"]) or g["n"]
            if hechos > 1 and not ev.get("texto") and not cerrado:
                g["texto"] += (f" · {hechos} facturas"
                               if ev["tipo"] == "retencion_asinfo_aplicada"
                               else f" ({hechos})")
            g["titulo"] = ev["label"] + (f" · {ev['concepto']}"
                                         if ev.get("concepto") else "")
            # Al Historial, filtrado por ese tipo y ese día: ahí están los
            # movimientos uno por uno, que es como a la dueña le gusta verlos.
            # `banco_varios` no es un tipo de `mov_doble` — filtrar por él daría
            # una pantalla vacía, así que ese renglón no lleva link.
            g["url"] = (None if ev["tipo"] == "banco_varios" else
                        f"/historial?tipo={ev['tipo']}"
                        f"&desde={ev['dia']}&hasta={ev['dia']}")

        elif g["n"] > 1:
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
            if g.get("familia") == "sin_explicar":
                g["texto"] = (f"{ETIQUETAS.get(g.get('col'), '')}: "
                              f"sin explicar por documento").strip(": ")
            if nombres and g.get("familia") != "sin_explicar":
                g["texto"] += f" · {nombres}"
            g["url"] = None                    # son varios: ninguna ficha sola
        elif g.get("familia") == "sin_explicar":
            g["texto"] = (f"{ETIQUETAS.get(g.get('col'), g.get('col') or '')}"
                          f": sin explicar por documento").strip(": ")
        else:
            g["texto"] = _corto_etiqueta(g.get("etiqueta") or g["regla"])
        g["texto"] = _abreviar_etapas(g.get("texto") or "")
        out.append(g)
    if abs(menores) >= UMBRAL_VISIBLE:
        out.append({"texto": "movimientos chicos", "aporte": menores, "n": 0,
                    "url": None, "col": None, "por_col": {}, "bruto": 0.0,
                    "familia": "utilidad"})
    if d_utilidad is not None:
        resto = round(d_utilidad - sum(g["aporte"] for g in out), 2)
        if abs(resto) >= UMBRAL_VISIBLE:
            out.append({"texto": "diferencia contra el Δ", "aporte": resto, "n": 0,
                        "url": None, "col": None, "por_col": {}, "bruto": 0.0,
                        "familia": "sin_explicar"})
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


    # 🚨 "Sin movimientos" y "sin registro" NO son lo mismo. Una ventana en la
    # que de verdad no se movió nada y una anterior a que existiera la
    # grabadora de detalle se ven idénticas —las dos con la lista vacía— y
    # decirle a la dueña "no se movió ningún documento" sobre una ventana de
    # 2.000 dólares es mentirle.
    desde = _desde_cuando_hay_detalle()
    fila["sin_registro"] = bool(desde is None or id_traza < desde)
    # Una foto vieja con movimientos es una foto RECONSTRUIDA: lo que hay salió
    # de `fecha_crea`, no de un diff, y le falta todo lo que se aplicó.
    fila["reconstruido"] = bool(fila["sin_registro"] and movs)

    total = round(sum(float(m.get("aporte") or 0) for m in movs), 2)
    fila["explicado"] = total
    # A una foto sin registro —o reconstruida, que es lo mismo con más datos—
    # no se le puede exigir que cierre: le falta lo aplicado por definición.
    fila["residuo"] = (
        None if (fila["sin_registro"] or fila.get("d_utilidad") is None)
        else round(float(fila["d_utilidad"]) - total, 2))
    fila["ciegos"] = [m for m in movs if m.get("familia") == "sin_explicar"]

    # ⭐ El resumen se arma acá, DESPUÉS de saber si la foto es vieja: a una
    # reconstruida no se le agrega el renglón de "sin explicar por documento",
    # porque lo que falta es, justamente, lo que no se puede saber — y el
    # cartel ámbar de arriba ya lo dice.
    from modules.informes import eventos as _ev

    _desde = (fila.get("anterior") or {}).get("creado_en")
    _hasta = fila.get("creado_en")
    idx = _ev.indice(_ev.de_la_ventana(_desde, _hasta),
                     _ev.transacciones(_desde, _hasta))
    fila["resumen"] = resumir(
        movs, None if fila["sin_registro"] else fila.get("d_utilidad"), idx)
    fila["d_kg"] = fila.get("d_kg") or {}
    return fila
