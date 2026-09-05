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

import json as _json
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
    _ins = hil.get("insumos") or {}
    fila = {
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
        # 🚨 El $/kg de cada etapa (mig 0185). Hasta hoy sólo se guardaba el
        # del hilado y por eso "el valor de tejido y terminado no se podía
        # reconstruir": con el de terminado, cada venta puede decir su margen
        # sin esperar a que Asinfo descuente el stock (TMT 2026-08-10).
        "tejido_ukg": _f(tejido, "ukg"),
        "terminado_kg": _f(term, "kg"),
        "terminado_ukg": _f(term, "ukg"),
        "compras_kg": _f(hil, "compras"),
        "compras_us": _f(hil, "compras_us"),
        "kg_sin_costo": _f(hil, "kg_sin_costo"),
        # De qué está hecho `compras_us` (mig 0244): con el desglose, un salto
        # del $/kg se puede nombrar por su causa. Tamara 05/09/2026.
        "compras_import_us": _f(_ins, "import_us"),
        "compras_local_us": _f(_ins, "local_us"),
        "al_precio_us": _f(_ins, "al_precio_us"),
        "recargos_tardios_us": _f(_ins, "recargos_tardios_us"),
        "hilado_insumos": (_json.dumps(_ins, default=str, ensure_ascii=False)
                           if _ins else None),
        "venta_kg": _f(kg, "kvent"),
        "venta_us": _f(kg, "uvent"),
    }
    # ⭐ La foto se fecha cuando se LEYÓ el saldo, no cuando se grabó. La
    # ventana de nombres de bancos (`eventos.transacciones`) se corta por
    # `creado_en`; si el borde es la grabación, todo lo cargado mientras el
    # balance se calculaba (más de un minuto el 05/09 con el server lento) se
    # nombra en una foto y su plata aparece en la siguiente: "sin explicar
    # +4.355" y a los diez minutos "−4.355". Sin `leido_en` (balance viejo,
    # tests) queda el DEFAULT de la tabla.
    if bal.get("leido_en") is not None:
        fila["creado_en"] = bal["leido_en"]
    return fila


def foto_stock_buena() -> dict | None:
    """La última foto con el stock LEÍDO DE VERDAD (kg por etapa + $/kg).

    TMT 2026-08-12 (dueña: *"tomar los kilos de la última foto de la traza,
    exactamente esto quiero"*). Cuando Asinfo no contesta el inventario, el
    balance caía a los kilos estilo dBase — apertura del mes ± movimientos —
    y eso mueve el stock ~460.000 US$ de un saque, hacia arriba. Es la misma
    trampa que el $/kg: en vez de quedarse quieto, el balance se agarra de
    otra base. Acá está el ancla persistente (la traza sobrevive al reinicio,
    la memoria del proceso no).

    "Buena" = con `compras_us > 0`: las compras del mes vienen por el mismo
    puente que el inventario, así que una foto que las tiene es una foto que
    se sacó con Asinfo vivo. Sirve dentro del MES en curso: los kilos de un
    mes anterior no son el stock de hoy.

    Devuelve None si no hay ninguna (mes recién arrancado, tabla vacía): ahí
    el balance sigue con el camino de siempre. Nunca lanza.
    """
    try:
        return db.fetch_one(
            """
            SELECT creado_en, hilado_kg, hilado_ukg, tejido_kg, terminado_kg
              FROM scintela.traza_utilidad
             WHERE creado_en >= date_trunc(
                       'month', (CURRENT_TIMESTAMP - INTERVAL '5 hours'))
               AND COALESCE(compras_us, 0) > 0
               AND COALESCE(hilado_kg, 0) > 0
               AND COALESCE(hilado_ukg, 0) > 0
             ORDER BY creado_en DESC, id_traza DESC
             LIMIT 1
            """
        )
    except Exception as e:  # noqa: BLE001 -- la grabadora no puede tumbar nada
        _LOG.warning("traza_utilidad: no pude leer la foto de stock (%s)", e)
        return None


def _muy_reciente(conn, min_gap_secs: int) -> bool:
    """¿La foto anterior es de hace menos de un intervalo?

    🚨 TMT 2026-08-25 (dueña): *"hay algo raro, la traza no se está
    moviendo"*. No era que no se movía: desde el 24/08 CADA foto se guardaba
    DOS veces, con dos a diez segundos de diferencia (173 de las 357 fotos del
    25/08). La segunda no tenía nada nuevo que contar, así que la mitad de los
    renglones de la pantalla salían con Δ 0 y las columnas vacías —la grilla
    parecía congelada— y las 200 fotos que muestra llegaban nueve horas atrás
    en vez de dos días.

    ⭐ El freno de cinco minutos era una VARIABLE DE PROCESO (`_ultimo_ts`) y
    hay más de un proceso sirviendo la app: cada uno respetaba su propio reloj.
    El 24/08 el ciclo de fondo se partió en dos hilos y los relojes quedaron
    alineados, así que lo que antes se cruzaba de a ratos pasó a cruzarse
    siempre. El freno tiene que vivir en la BASE, que es lo único que los
    procesos comparten —igual que la explicación del día, que se frena con su
    índice único y no con una variable—, y se pregunta ADENTRO del lock: entre
    la pregunta y el INSERT no puede entrar nadie.
    """
    fila = db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MAX(creado_en)))"
        " AS seg FROM scintela.traza_utilidad", conn=conn)
    seg = (fila or {}).get("seg")
    return seg is not None and float(seg) < min_gap_secs


def registrar(origen: str = "manual", bal: dict | None = None,
              momento: str = "foto", min_gap_secs: int = 0) -> dict:
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
            if min_gap_secs and _muy_reciente(conn, min_gap_secs):
                res["motivo"] = "ya hay una foto de hace menos de un intervalo"
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
        _avisar_que_no_guarda(res["motivo"])
    return res


def _avisar_que_no_guarda(motivo: str) -> None:
    """La grabadora no puede tumbar nada, pero tampoco puede callarse.

    🚨 TMT 2026-08-10, después de que la traza dejara de guardar diez minutos
    por una columna que no existía: *"y también algo que avise si no está
    guardando"*. El fail-soft estaba bien —la app no se cae por una foto— pero
    el único rastro era una línea de log que no mira nadie: el error se
    escribió cien veces y la pantalla siguió como si nada.

    ⭐ La clave lleva el DÍA: el hilo reintenta cada seis minutos y sin eso el
    buzón se llenaría de cien avisos iguales. Uno por día alcanza para que se
    entere.
    """
    try:
        from filters import today_ec
        from modules import avisos as _av
        _av.avisar(fuente="traza", nivel="alerta",
                   titulo="La traza dejó de guardar fotos",
                   detalle=(motivo or "")[:200], url="/informes/traza",
                   clave=f"traza:falla:{today_ec()}")
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe al que avisa
        _LOG.warning("traza_utilidad: no pude avisar la falla (%s)", e)


def registrar_si_toca(origen: str = "loop") -> bool:
    """Llamada desde el ciclo de fondo: guarda si pasó el intervalo."""
    global _ultimo_ts
    ahora = time.time()
    if (ahora - _ultimo_ts) < _intervalo():
        return False
    _ultimo_ts = ahora
    # El reloj del proceso es sólo el filtro barato; el freno de verdad lo pone
    # la base (`_muy_reciente`), que es la que ven todos los procesos.
    return bool(registrar(origen=origen, min_gap_secs=_intervalo()).get("ok"))


# ── Lectura ────────────────────────────────────────────────────────────────

#: Columnas que se comparan foto contra foto para decir "esto fue lo que se
#: movió". `totp` va con el signo dado vuelta porque es pasivo: si sube, la
#: utilidad baja.
COMPONENTES = (
    ("caja", 1), ("bancos", 1), ("cheques", 1), ("facturas", 1),
    ("antic", 1), ("vsto", 1), ("vqx", 1), ("umaq", 1), ("uact", 1),
    ("totp", -1), ("uret", 1),
    # ⭐ Tamara 2026-09-02 — el DOCEAVO, y el que faltaba.
    #
    # Los once de arriba son `patr`. Pero la utilidad es `patr − PATANT`, y
    # PATANT (el patrimonio del último cierre, scintela.historia) no estaba en
    # la lista. Cuando cambia, el Δ de la utilidad se mueve y las once celdas
    # quedan VACÍAS: el síntoma exacto de "no cierra", sin nada que mirar.
    #
    # Pasó el 02/09/2026: entre las 08:02 y las 08:20 la utilidad saltó a
    # +1.971.282 y volvió a −709.403 (Δ +2.679.072 y −2.677.092) con la única
    # celda escrita en Stock, +1.450 y +531. No se movió NADA del patrimonio:
    # se movió el cierre contra el que se compara —una regeneración de snapshot
    # borra la fila y la vuelve a insertar, y en esa ventana `historia_ultimo_mes`
    # devuelve un cierre más viejo—. Durante esos 18 minutos el balance mostró
    # 1,97 MILLONES de utilidad a quien lo abriera.
    #
    # No hace falta migración: PATANT sale de lo que la foto YA guarda,
    # `patr_neto + uret − utilidad` (ver `_patant_de`). Signo −1, como los
    # pasivos: si el cierre anterior sube, la utilidad baja.
    ("patant", -1),
)

ETIQUETAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheques",
    "facturas": "Facturas", "antic": "Anticipos", "vsto": "Stock MP+Prod.",
    "vqx": "Stock Químicos", "umaq": "Maquinaria", "uact": "Terrenos/Edif.",
    "totp": "Pasivos", "uret": "Dividendos",
    "patant": "Cierre anterior (PATANT)",
}


def _patant_de(fila: dict) -> float | None:
    """PATANT de una foto, despejado de lo que la foto ya guarda.

        utilidad = patr − patant        y     patr = patr_neto + uret
        ⇒ patant = patr_neto + uret − utilidad

    Devuelve None si a la foto le falta alguno de los tres (las anteriores a
    que existiera `patr_neto` no se pueden reconstruir). None NO es 0: una foto
    sin el dato se saltea en el Δ en vez de inventar un movimiento gigante.
    """
    try:
        pn, ur, ut = fila.get("patr_neto"), fila.get("uret"), fila.get("utilidad")
        if pn is None or ut is None:
            return None
        return float(pn) + float(ur or 0) - float(ut)
    except (TypeError, ValueError):
        return None


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
COLUMNAS_OTROS = ("umaq", "uact", "uret", "patant")

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


#: Techo de fotos por rango. Un día son ~240; cinco días entran holgados y
#: 1.200 filas todavía se scrollean sin que el navegador sufra.
TOPE_RANGO = 1200


def entre(desde: str, hasta: str, n: int = TOPE_RANGO) -> list[dict]:
    """Las fotos de un rango de DÍAS (fechas de Ecuador), la más nueva primero.

    TMT 2026-08-09: *"¿cuán atrás podemos ir? porque quizás quiero ver un finde
    también"*. Guardadas están todas —nada se purga: 2.216 fotos desde el 31/07
    al 09/08, ~240 por día, sábados y domingos incluidos—; lo que faltaba era
    cómo pedirlas. La pantalla mostraba siempre las últimas 200, que son ~20 h.

    🚨 Trae UNA foto MÁS VIEJA que el rango, marcada `en_rango = False`.
    `con_deltas` compara cada foto contra la de abajo, así que sin ese ancla la
    primera foto del sábado saldría con el Δ vacío — y es justo la que dice qué
    pasó durante la noche del viernes. El que llama la descarta DESPUÉS de
    calcular los Δ.
    """
    try:
        n = max(1, min(TOPE_RANGO, int(n)))
    except (TypeError, ValueError):
        n = TOPE_RANGO
    try:
        return db.fetch_all(
            f"""
            WITH rango AS (
                SELECT *
                  FROM scintela.traza_utilidad
                 WHERE (creado_en AT TIME ZONE 'America/Guayaquil')::date
                       BETWEEN %s AND %s
                 ORDER BY creado_en DESC, id_traza DESC
                 LIMIT {n}
            ), ancla AS (
                SELECT *
                  FROM scintela.traza_utilidad
                 WHERE creado_en < (SELECT MIN(creado_en) FROM rango)
                 ORDER BY creado_en DESC, id_traza DESC
                 LIMIT 1
            )
            SELECT x.*,
                   TO_CHAR(x.creado_en AT TIME ZONE 'America/Guayaquil',
                           'DD/MM HH24:MI') AS cuando,
                   ((x.creado_en AT TIME ZONE 'America/Guayaquil')::date
                    BETWEEN %s AND %s) AS en_rango
              FROM (SELECT * FROM rango UNION ALL SELECT * FROM ancla) x
             ORDER BY x.creado_en DESC, x.id_traza DESC
            """, (desde, hasta, desde, hasta)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no se pudo leer el rango (%s)", e)
        return []


def primera_foto() -> str | None:
    """El día de la foto más vieja que hay guardada (para el `min` del input).

    Sin esto la pantalla deja elegir 2019 y devuelve una tabla vacía sin decir
    por qué: la grabadora arrancó el 31/07/2026 y antes no hay nada.
    """
    try:
        r = db.fetch_all(
            """
            SELECT MIN((creado_en AT TIME ZONE 'America/Guayaquil')::date) AS d
              FROM scintela.traza_utilidad
            """) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("traza_utilidad: no se pudo leer la primera foto (%s)", e)
        return None
    d = (r[0] or {}).get("d") if r else None
    return d.isoformat() if d else None


def con_deltas(filas: list[dict]) -> list[dict]:
    """Agrega a cada foto el Δ contra la ANTERIOR (la de abajo en la lista) y
    quién explica ese Δ.

    `filas` viene de `ultimas()`: más nueva primero. La última no tiene contra
    qué compararse y queda con delta None.
    """
    # PATANT es derivado, no una columna guardada: se calcula una vez por foto
    # y viaja en la fila para que el Δ lo trate como un componente más (y para
    # que el detalle pueda mostrarlo). Ver `_patant_de`.
    filas = [dict(f, patant=_patant_de(f)) for f in (filas or [])]

    out = []
    for i, f in enumerate(filas):
        fila = dict(f)
        prev = filas[i + 1] if (i + 1) < len(filas) else None
        if not prev:
            fila["d_utilidad"] = None
            fila["movio"] = []
            fila["movidas"] = set()
            fila["delta"] = {}
            fila["d_kg"] = {}
            fila["d_ukg"] = None
            out.append(fila)
            continue
        try:
            fila["d_utilidad"] = round(
                float(f.get("utilidad") or 0) - float(prev.get("utilidad") or 0), 2)
        except (TypeError, ValueError):
            fila["d_utilidad"] = None
        movs = []
        for col, signo in COMPONENTES:
            # PATANT es derivado y puede faltar en fotos viejas. Sin las dos
            # puntas no hay Δ que calcular: saltear es correcto, tratar el None
            # como 0 inventaría un movimiento del tamaño del patrimonio.
            if col == "patant" and (f.get(col) is None or prev.get(col) is None):
                continue
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
        fila["d_ukg"] = _d_ukg(f, prev)
        out.append(fila)
    return out


def _d_ukg(f: dict, prev: dict | None) -> float | None:
    """Cuánto cambió el $/kg del hilado entre las dos fotos.

    🚨 TMT 2026-08-11: *"podrías poner el cambio de $/kg en su columna +x"*. La
    columna $/kg mostraba el NIVEL (3,0443) y el cambio vivía en una nota gris
    del renglón ("cambió el $/kg de 3 etapas"): para saber de cuánto había que
    restar dos filas a ojo. Es el número que revalúa TODO el stock de un saque,
    así que va con su signo en su columna.

    Cuatro decimales porque es la escala en la que se mueve: a dos, +0,0027
    sería "+0,00".
    """
    if not prev:
        return None
    try:
        d = round(float(f.get("hilado_ukg") or 0)
                  - float(prev.get("hilado_ukg") or 0), 4)
    except (TypeError, ValueError):
        return None
    return d or None


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


def _url_del_grupo(g: dict, ev: dict) -> str | None:
    """A dónde lleva el renglón al clickearlo.

    🚨 TMT 2026-08-11: *"cuando lo clickeo me debería llevar sólo a esas
    facturas"*. Iba a `/historial?tipo=…&desde=hoy&hasta=hoy` — las 37 facturas
    del día para explicar cuatro. Un link que obliga a buscar la fila a ojo no
    está terminado.

    Ahora van los `id_mov_doble` EXACTOS del grupo. El filtro por tipo y día
    queda de respaldo para las fotos anteriores a la grabadora de detalle, que
    no los guardaron: ahí un link amplio es mejor que ninguno.

    `banco_varios` no es un tipo de `mov_doble` — filtrar por él daría una
    pantalla vacía, así que ese renglón sigue sin link.
    """
    # `banco_varios` sigue sin link aunque traiga hechos: es un renglón que
    # junta movimientos de varias cuentas y mandar a un subconjunto engaña.
    if (ev or {}).get("tipo") == "banco_varios":
        return None
    ids = ",".join(str(h) for h in sorted(g.get("hechos") or []) if h)
    if ids:
        return f"/historial?ids={ids}"
    # 🚨 TMT 2026-08-12: el respaldo filtra el historial POR TIPO, y varios
    # renglones de la traza llevan etiquetas que el historial no conoce
    # ("banco_cargado", "banco_varios"): el link abría una pantalla vacía —
    # roto sin dar 404. En vez de ir tachándolos a mano —así se coló
    # `banco_cargado`— se le PREGUNTA al historial si conoce el tipo. El de
    # banco manda el suyo por documento (`banco_de_directo`…) en
    # `tipo_historial`.
    from modules.historial.queries import TIPOS_LABEL

    tipo = (ev or {}).get("tipo_historial") or (ev or {}).get("tipo")
    if not tipo or tipo not in TIPOS_LABEL:
        return None
    return (f"/historial?tipo={tipo}"
            f"&desde={ev['dia']}&hasta={ev['dia']}")


def _nombres(g: dict, tope: int = 3) -> str:
    """"CLR ×2, MHE, MPO" — los que más pesan, con el ×N si alguno repite.

    🚨 TMT 2026-08-11: *"4 facturas pero acá dicen solo 3 nombres"*. `quienes`
    es un dict por cliente, así que dos facturas del mismo cliente colapsan en
    un nombre y el renglón queda peleando con su propio número. El ×2 lo cierra
    sin alargar el texto ni mentir el conteo.
    """
    orden = sorted(g.get("quienes") or {},
                   key=lambda k: abs((g["quienes"] or {})[k]), reverse=True)
    # El ×N sólo cuando hay VARIOS nombres. Con uno solo el renglón ya dice
    # cuántos son ("8 CH → BC · CLI") y el ×8 es ruido que repite el número.
    cuantos = (g.get("cuantos") or {}) if len(orden) > 1 else {}
    partes = [f"{k} ×{cuantos[k]}" if cuantos.get(k, 1) > 1 else k
              for k in orden[:tope]]
    txt = ", ".join(partes)
    if len(orden) > tope:
        txt += f" +{len(orden) - tope}"
    return txt


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

ROTULO_JUNTADO = {
    "factura_emitida": ("FA", ""),
    "cheque_aplicado_a_factura": ("CH → FA", ""),
    # Segunda pasada, 07/08: las devoluciones caían de a una (10 renglones en
    # el día, cuatro de ellas en la MISMA ventana que las 13 facturas nuevas)
    # y las compras de una importación también (tres "CP AQ → DE 1013x"
    # seguidas). Van aparte de las facturas nuevas: el signo es al revés y
    # mezclarlas escondería una devolución grande adentro de un neto.
    "factura_devolucion": ("FA dev", ""),
    "compra_a_posdat": ("CP → DE", ""),
    "posdat_restaurada": ("CP → DE", "restaurada"),
    # 🚨 TMT 2026-08-07, viendo ocho renglones seguidos de "RT TNZ ✗ FA":
    # *"esto también juntalo, retenciones es un solo movimiento"*. La
    # DESaplicada no lleva `batch_id` —la aplicada sí, por eso ésa ya salía en
    # un renglón— así que caía de a una.
    # 🚨 TMT 2026-08-07, viendo cuatro renglones "retenciones · 17 / · 4 / · 2"
    # en la misma ventana: *"se nos separaron de vuelta"*. La APLICADA lleva
    # `batch_id` —una por corrida del cron— y con eso se juntaba… por batch.
    # Si el cron corre tres veces en cinco minutos son tres renglones que
    # dicen lo mismo. Se juntan por TIPO, como todo lo demás.
    "retencion_asinfo_aplicada": ("retenciones", "facturas"),
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


#: (hecho, componente) donde el hecho NO puede explicar que el documento SALGA
#: de ese componente.
#:
#: 🚨 TMT 2026-08-07, viendo "8 CH → FA · KRH" con −$7.000 en Cheq.: aplicar un
#: cheque a una factura **no lo saca de cartera**. Si Cheq. bajó es porque los
#: cheques se depositaron — y el depósito se cargó a mano por la pantalla de
#: Bancos, que no deja `mov_doble`, así que el único hecho pegado al documento
#: era el "aplicado a factura" de cuando entró. La traza le puso ese nombre a
#: plata que se fue al banco: no es un nombre pobre, es un nombre FALSO.
#:
#: Cuando el hecho contradice lo que el diff vio, gana el diff: "8 cheques
#: depositados" dice menos, pero no miente.
HECHO_NO_EXPLICA_BAJA = {
    ("cheque_aplicado_a_factura", "cheques"),
    ("cheque_creado", "cheques"),
    ("factura_emitida", "facturas"),
}

#: El código de dos letras de cada componente, para nombrar un traspaso por sus
#: dos patas: "CH → BC". Es el mismo vocabulario de `TIPOS_CORTO`.
COD_COMPONENTE = {
    "caja": "CJ", "bancos": "BC", "cheques": "CH", "facturas": "FA",
    "antic": "AN", "totp": "DE", "uret": "DV", "vsto": "STK", "vqx": "QX",
}


def _unir_las_dos_patas(grupos: dict) -> None:
    """Dos renglones que son las dos patas del MISMO movimiento van en uno.

    🚨 TMT 2026-08-07: *"esto también podría ser un renglón, ¿te das cuenta?"*,
    sobre "depósito · 1 ch.KRH +7.000" y "8 CH → FA · KRH −7.000".

    Cuando el hecho tiene `mov_doble` los dos lados ya caen en el mismo grupo
    (es lo que hace `eventos._cuentas`). Esto es para cuando NO lo tiene: la
    única señal que queda es que los importes son exactamente opuestos y tocan
    componentes distintos. Se une SÓLO si el par es inequívoco —un solo
    candidato de cada lado—: unir de más sería inventar un hecho.
    """
    # ⭐ 07/08, segunda vuelta: la condición era `len(por_col) == 1` y con eso
    # la cobranza que se deposita —que toca cheques Y facturas— nunca entraba,
    # justo el caso para el que esto se escribió. Lo que importa no es cuántos
    # componentes toca cada lado sino que NO COMPARTAN ninguno: si comparten,
    # no son dos patas de un traspaso.
    candidatos = [g for g in grupos.values()
                  if abs(g["aporte"]) >= UMBRAL_VISIBLE
                  and g.get("familia") == "traspaso"
                  and g["por_col"]]
    usados: set[int] = set()
    for a in candidatos:
        if id(a) in usados or a["aporte"] <= 0:
            continue
        pares = [b for b in candidatos
                 if id(b) not in usados and b is not a
                 and abs(a["aporte"] + b["aporte"]) < 0.01
                 and not (set(a["por_col"]) & set(b["por_col"]))]
        if len(pares) != 1:
            continue                          # ambiguo: no se inventa
        b = pares[0]
        usados.update({id(a), id(b)})
        destino, origen = a, b                # a recibe, b entrega
        def _mayor(g):
            return max(g["por_col"], key=lambda k: abs(g["por_col"][k]))

        cod_o = COD_COMPONENTE.get(_mayor(origen), "")
        cod_d = COD_COMPONENTE.get(_mayor(destino), "")
        if not (cod_o and cod_d):
            continue
        quienes = dict(origen["quienes"])
        for k, v in origen.get("cuantos", {}).items():
            destino.setdefault("cuantos", {})
            destino["cuantos"][k] = destino["cuantos"].get(k, 0) + v
        for k, v in destino["quienes"].items():
            quienes[k] = quienes.get(k, 0.0) + v
        nombres = ", ".join(sorted(quienes, key=lambda k: abs(quienes[k]),
                                   reverse=True)[:3])
        n = max(len(origen["hechos"]), origen["n"])
        destino["texto_unido"] = (
            f"{n if n > 1 else ''} {cod_o} → {cod_d}".strip()
            + (f" · {nombres}" if nombres else ""))
        destino["aporte"] = round(destino["aporte"] + origen["aporte"], 2)
        destino["por_col"].update(origen["por_col"])
        destino["n"] += origen["n"]
        destino["hechos"] |= origen["hechos"]
        destino["url"] = destino.get("url") or origen.get("url")
        origen["fundido"] = True


#: La etiqueta sintética que escribe `foto._det_antic` al cruzar los anticipos
#: con el stock de Asinfo. Es el ÚNICO renglón de `antic` que no es un
#: documento, así que sirve de ancla para reconocer el par.
TXT_ANTICIPO_RECIBIDO = "Menos anticipos cuya mercadería ya está en stock"


def _importacion_del_anticipo(monto: float, hasta) -> str:
    """"AC 33" — la importación que explica ese anticipo, o "" si no es una sola.

    🚨 El anticipo baja cuando **Asinfo** muestra la mercadería recibida, y la
    compra la crea el automático DESPUÉS (medido el 10/08: el anticipo cayó en
    la foto de las 11:26 y la compra `bap-auto` nació 11:38). Por eso no se
    busca por la ventana sino por el IMPORTE, que coincide al centavo.
    ⭐ Un solo candidato o no se nombra: nombrar de más sería inventar.
    """
    if not monto or not hasta:
        return ""
    try:
        filas = db.fetch_all(
            """
            SELECT codigo_prov, concepto
              FROM scintela.compra
             WHERE usuario_crea LIKE 'bap%%'
               AND ABS(importe - %s) < 0.01
               AND fecha_crea BETWEEN %s - interval '5 days' AND %s + interval '5 days'
            """, (abs(monto), hasta, hasta)) or []
    except Exception as e:  # noqa: BLE001 -- un renglón nunca rompe la pantalla
        _LOG.warning("traza: no pude nombrar la importación (%s)", e)
        return ""
    if len(filas) != 1:
        return ""
    cod = (filas[0].get("codigo_prov") or "").strip().upper()
    ref = " ".join((filas[0].get("concepto") or "").split())
    return f"{cod} {ref}".strip()


def _num(v, dec: int) -> str:
    from filters import num_es
    return num_es(v, dec)


def _nota_del_margen(venta: dict | None) -> str:
    """"3.100 kg a $ 3,03 el kilo de terminado → margen 3.107 (25%)".

    🚨 TMT 2026-08-10: *"¿se puede? porque Asinfo saca un poco más tarde el
    stock real que la factura"*. Sí, pero NO pareando: medido, sólo 68 de 137
    ventanas con facturación tienen la salida de stock en la misma ventana. El
    margen sale de la venta misma —kg × $/kg de terminado— así que no depende
    del reloj de Asinfo.

    ⭐ Es un margen APROXIMADO: el costo es el promedio del terminado, no el de
    esa tela en particular. Y sólo existe desde la mig 0185: las fotos viejas
    no guardaron el precio.
    """
    if not venta:
        return ""
    kg, us, ukg = venta.get("kg") or 0, venta.get("us") or 0, venta.get("ukg") or 0
    if kg <= 0 or us <= 0 or ukg <= 0:
        return ""
    costo = kg * ukg
    margen = us - costo
    pct = round(margen / us * 100) if us else 0
    return (f"{_num(kg, 0)} kg a $ {_num(ukg, 4)} el kilo de terminado → "
            f"margen {_num(margen, 0)} ({pct}%)")


def _texto_mercaderia(n_anticipos) -> str:
    """"entró la mercadería de 4 anticipos" — el número, igual que en el pase.

    🚨 TMT 2026-08-11: *"me gusta el 4 para saber que había 4 distintos"*. Una
    importación se paga en cuotas y cada una es un anticipo suyo.
    """
    n = int(n_anticipos or 0)
    return (f"entró la mercadería de {n} anticipos" if n > 1
            else "entró la mercadería del anticipo")


def _ancla_del_anticipo(grupos: dict) -> tuple[dict | None, dict | None]:
    """El renglón del anticipo que se volvió mercadería, y su evento si lo hay.

    Hay DOS maneras de que el anticipo se vaya, según qué tan rápido pase todo:

    - Asinfo muestra la mercadería recibida y el descuento sintético
      (`TXT_ANTICIPO_RECIBIDO`) tapa el anticipo; la compra `bap-auto` nace
      después. Ése es el caso del 10/08 (anticipo 11:26, compra 11:38).
    - 🚨 TMT 2026-08-14, ventana de las 08:40 (AC 38): las dos cosas caen
      DENTRO de la misma foto de 5 minutos, así que el descuento sintético
      nunca llega a aparecer y lo que se ve es el pase a la compra
      (`bap_anticipo_a_compra`) −74.769 contra el hilo que entra +74.379.
      *"me gustaría que la entrada de hilo aparezca arriba con el anticipo"*.

    Es el mismo hecho con dos anclas distintas. El sintético manda: si está,
    el pase es la OTRA formalidad y la une `_unir_conversion_del_anticipo`.
    """
    ant = next((g for g in grupos.values()
                if (g.get("etiqueta") or "") == TXT_ANTICIPO_RECIBIDO
                and not g.get("fundido")), None)
    if ant:
        return ant, None
    conv = next((g for g in grupos.values()
                 if ((g.get("evento") or {}).get("tipo")
                     == "bap_anticipo_a_compra")
                 and not g.get("fundido")), None)
    return (conv, conv.get("evento")) if conv else (None, None)


def _unir_anticipo_con_mercaderia(grupos: dict, hasta=None) -> None:
    """El anticipo que sale y la mercadería que entra son UN movimiento.

    🚨 TMT 2026-08-10, sobre una ventana con −73.984 en Ant. y +75.026 en Stk.:
    *"en el mismo bucket entra mercadería y sale anticipo, ¿no?"*. Sí: el
    anticipo de la importación se volvió stock. Salían tres renglones —el
    anticipo, la tela y la revaluación del $/kg— y había que sumarlos a ojo
    para ver que era una sola cosa.

    ⭐ La revaluación entra en el MISMO renglón a propósito. Partirla en
    "entró tanto" y "se revaluó tanto" hacía aparecer una diferencia
    ("entró 4.111 abajo del anticipo") que NO es un hecho: es la aritmética
    del promedio ponderado. Los kilos entraron a $3,2334 con el stock a
    $3,0405, así que parte del costo se reparte sobre lo que ya había. El
    $/kg queda como nota al pie, que es lo que explica el signo.
    """
    ant, ev = _ancla_del_anticipo(grupos)
    tela = next((g for g in grupos.values()
                 if g.get("regla") == "Stock" and not g.get("fundido")), None)
    if not ant or not tela or ant["aporte"] >= 0 or tela["aporte"] <= 0:
        return
    tarifa = next((g for g in grupos.values()
                   if g.get("regla") == "Revaluación de stock"
                   and not g.get("fundido")), None)
    if ev:
        # El renglón de la conversión ya sabe cómo se llama la importación y
        # en cuántos anticipos vino: no hay que salir a buscarla por importe.
        nombre = _nombre_de_fabrica(ev.get("concepto") or "")
        que = _texto_mercaderia((ev.get("meta") or {}).get("n_anticipos"))
        ant["texto_unido"] = f"{nombre} · {que}" if nombre else que
    else:
        cod = _importacion_del_anticipo(ant["aporte"], hasta)
        ant["texto_unido"] = ("entró la mercadería del anticipo " + cod if cod
                              else "entró la mercadería de los anticipos")
    for otro in (tela, tarifa):
        if not otro:
            continue
        ant["aporte"] = round(ant["aporte"] + otro["aporte"], 2)
        for c, v in otro["por_col"].items():
            ant["por_col"][c] = round(ant["por_col"].get(c, 0.0) + v, 2)
        ant["n"] += otro["n"]
        otro["fundido"] = True
    # Los kilos se pintan en el renglón del stock: éste ahora ES el del stock.
    ant["col"] = "vsto"
    ant["bruto"] = max((abs(v) for v in ant["por_col"].values()), default=0.0)
    # El $/kg no desaparece: baja a la nota, que es lo que explica el signo.
    if tarifa:
        ant["nota"] = tarifa.get("etiqueta") or ""


def _nombre_de_fabrica(concepto: str) -> str:
    """"AI 16" — el pedazo del concepto que la fábrica reconoce.

    `dolares.queries.convertir_a_compra` ya escribe el concepto empezando por
    ahí ("AI 16 · 4 anticipo(s) → compra #10148"), justo porque el 31/07 la
    dueña preguntó *"acá nada dice AC 22, ¿cómo sé qué anticipo es?"*. La traza
    no lo usaba: armaba el suyo con el tipo y salía "AN AI → CP 10148" — el
    código del proveedor sin el número, y un número que es el de la compra.
    """
    return ((concepto or "").split("·")[0].split("—")[0]).strip()


def _texto_conversion(tipo: str, n_anticipos) -> str:
    """"4 anticipos pasaron a la compra" — el número va porque son documentos
    distintos.

    🚨 TMT 2026-08-11: *"me gusta el 4 para saber que había 4 distintos"*. Una
    importación se paga en varias cuotas y cada una es un anticipo suyo; el
    total ya está en la columna, lo que el número agrega es en cuántos pedazos
    vino. Con uno solo no se dice "1": se dice "el anticipo".
    """
    n = int(n_anticipos or 0)
    cuantos = f"{n} anticipos" if n > 1 else "el anticipo"
    if tipo == "bap_anticipo_a_compra":
        return (f"{cuantos} pasaron a la compra" if n > 1
                else "el anticipo pasó a la compra")
    return f"se deshizo el pase de {cuantos} a la compra"


def _unir_conversion_del_anticipo(grupos: dict) -> None:
    """El anticipo que se vuelve compra es UN renglón, y no mueve plata.

    🚨 TMT 2026-08-11, mirando la ventana de las 09:52: dos renglones que se
    anulan —"Menos anticipos cuya mercadería ya está en stock" +73.127 y
    "AN AI → CP 10148 (4)" −73.127— y ninguno de los dos nombra la
    importación. *"no sé qué es CP y ese número, quiero ver el AI 16"*.

    Los dos son la MISMA formalidad: el anticipo sale de `dolares` y, en el
    mismo instante, el descuento sintético que lo tapaba deja de aplicar. La
    plata ya se había movido antes, cuando Asinfo marcó la recepción —eso es el
    otro renglón, el de `_unir_anticipo_con_mercaderia`—.

    ⭐ El renglón queda con aporte CERO y se muestra igual: `bruto` guarda lo
    que movió. Esconderlo por chico dejaría la conversión sin rastro, y es lo
    que la campanita acaba de avisar.
    """
    ant = next((g for g in grupos.values()
                if (g.get("etiqueta") or "") == TXT_ANTICIPO_RECIBIDO
                and not g.get("fundido") and g["aporte"] > 0), None)
    conv = next((g for g in grupos.values()
                 if ((g.get("evento") or {}).get("tipo")
                     == "bap_anticipo_a_compra")
                 and not g.get("fundido") and g["aporte"] < 0), None)
    if not ant or not conv or abs(ant["aporte"] + conv["aporte"]) >= 0.01:
        return
    bruto = max(ant["bruto"], conv["bruto"])
    ev = conv.get("evento") or {}
    nombre = _nombre_de_fabrica(ev.get("concepto") or "")
    que = _texto_conversion(ev.get("tipo") or "",
                            (ev.get("meta") or {}).get("n_anticipos"))
    conv["texto_unido"] = f"{nombre} · {que}" if nombre else que
    conv["nota"] = "no mueve plata: la mercadería ya había entrado"
    conv["aporte"] = round(conv["aporte"] + ant["aporte"], 2)
    for c, v in ant["por_col"].items():
        conv["por_col"][c] = round(conv["por_col"].get(c, 0.0) + v, 2)
    conv["n"] += ant["n"]
    conv["hechos"] |= ant["hechos"]
    conv["bruto"] = bruto              # aporta 0, pero movió 73.127
    ant["fundido"] = True


#: 🚨 TMT 2026-08-07, sobre el cuarto renglón más frecuente del día (19 de 200
#: ventanas): *"dice de qué sistema sale el dato, no qué pasó"*. Del lado de PC
#: el stock de químicos es UN número —no hay detalle por colorante— así que no
#: se puede decir cuál; lo que sí se sabe es para qué lado se movió, y se dice
#: con la misma gramática que la tela ("entró a", "salió de"). El nombre del
#: sistema queda en el `title`.
#:
#: ⭐ Se hace al RENDERIZAR y no en `foto._det_vqx`: la etiqueta se escribe
#: cuando se saca la foto, así que tocarla allá dejaría las 200 fotos ya
#: guardadas —las que se miran— diciendo lo de antes.
_TXT_QUIMICOS = "Stock de químicos (formulas_app)"


def _quimicos(texto: str, aporte: float) -> tuple[str, str]:
    """El renglón de químicos: (qué pasó con la plata, qué se cargó).

    🚨 TMT 2026-08-11: *"esto debería ser consumo en químicos, x órdenes
    terminadas"*, sobre "salió de químicos". Y es literal: el stock que PC lee
    de formulas_app es compras + ajustes − CONSUMO de las órdenes con
    `fecha_terminado` (`quimico_inv_formulas`), así que la baja ES eso.

    ⭐ Pero la causa NO se deduce del signo, y eso está medido: el 04/08 a las
    14:27 se cargó una compra a QSI de 60 u. y la ventana bajó −135,17 igual,
    porque el consumo de las órdenes que cerraron pesó más. Por eso el hecho
    —lo que se cargó— lo guarda la FOTO (`foto._causa_quimicos`, pegado al
    rótulo) y acá sólo se lo despega: arriba el neto, abajo la causa en gris.
    Las fotos anteriores al 11/08 no lo guardaron y quedan sin la nota.
    """
    if not texto.startswith(_TXT_QUIMICOS):
        return texto, ""
    causa = texto[len(_TXT_QUIMICOS):].lstrip(" ·").strip()
    neto = ("entró a químicos" if aporte > 0
            else "consumo de químicos por órdenes terminadas")
    return neto, causa


#: Por debajo de esto un insumo "no se movió": son centavos de redondeo.
_UMBRAL_INSUMO = 1.0


def _nombres_recargos(fila: dict, anterior: dict | None) -> list[str]:
    """Los códigos de importación ("AC 39") cuyos recargos entraron entre las
    dos fotos: los que están en `hilado_insumos` de la nueva y no en la vieja."""
    def _ids(f):
        ins = (f or {}).get("hilado_insumos")
        if isinstance(ins, str):
            try:
                ins = _json.loads(ins)
            except ValueError:
                ins = {}
        return {int(r.get("id_compra")): (r.get("codigo") or "").strip()
                for r in ((ins or {}).get("recargos_tardios") or [])
                if r.get("id_compra") is not None}
    nuevos, viejos = _ids(fila), _ids(anterior)
    out: list[str] = []
    for i in sorted(set(nuevos) - set(viejos)):
        c = nuevos[i]
        if c and c not in out:
            out.append(c)
    return out


def causa_tarifa(fila: dict | None, anterior: dict | None) -> str:
    """Por qué cambió el $/kg del hilado entre dos fotos, en una frase.

    Compara los cuatro insumos que la foto guarda desde la mig 0244 (plata de
    importaciones recibidas, de compras locales, del botón "Entrar al precio
    del hilo" y de recargos tardíos). Lo que subió es la causa; puede haber
    más de una en la misma ventana y se dicen todas. Sin desglose en alguna de
    las dos fotos (fotos viejas) devuelve "" y el renglón queda como estaba.
    """
    if not fila or not anterior:
        return ""
    def _d(col):
        a, b = anterior.get(col), fila.get(col)
        if a is None or b is None:
            return None
        return round(float(b) - float(a), 2)
    partes: list[str] = []
    d = _d("recargos_tardios_us")
    if d is not None and d >= _UMBRAL_INSUMO:
        nombres = _nombres_recargos(fila, anterior)
        quien = (" de " + _lista_y(nombres)) if nombres else ""
        partes.append(f"entraron al precio del hilo los recargos{quien} "
                      f"(+{_num(d, 2)})")
    d = _d("al_precio_us")
    if d is not None and d >= _UMBRAL_INSUMO:
        partes.append(f"una compra de hilo entró al precio a mano (+{_num(d, 2)})")
    d = _d("compras_local_us")
    if d is not None and d >= _UMBRAL_INSUMO:
        partes.append(f"entró una compra local de hilo (+{_num(d, 2)})")
    d = _d("compras_import_us")
    if d is not None and d >= _UMBRAL_INSUMO:
        partes.append(f"entró plata de una importación recibida (+{_num(d, 2)})")
    return " y ".join(partes)


def _lista_y(items: list[str]) -> str:
    """"AC 39", "AC 39 y MD 1", "AC 39, MD 1 y AI 30"."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " y " + items[-1]


def resumir(movs: list[dict], d_utilidad: float | None,
            eventos: dict | None = None, hasta=None,
            venta: dict | None = None, causa_tarifa: str = "") -> list[dict]:
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
        if (ev and m.get("tipo") == "baja"
                and (ev["tipo"], m.get("componente")) in HECHO_NO_EXPLICA_BAJA):
            ev = None                          # el hecho no explica la salida
        if ev:
            # 🚨 Dos hechos del MISMO tipo en la misma ventana se juntan
            # SIEMPRE: uno por hecho es la lista de documentos otra vez, que es
            # justo lo que la pantalla no quiere ser.
            #
            # La excepción es la cuenta bancaria: su renglón ya viene armado
            # (`ev["texto"]`) y su grupo ES la cuenta, no el tipo — juntar por
            # tipo mezclaría Pichincha con Internacional.
            clave = (("ev", ev["grupo"]) if ev.get("texto")
                     else ("tipo", ev["tipo"]))
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
                                  "cuantos": {},
                                  "evento": ev,
                                  "hechos": set(),
                                  "signos": set(),
                                  "familia": m.get("familia")})
        # 🚨 Cuántos HECHOS hay en el grupo, no cuántos documentos. Un cheque
        # aplicado a una factura toca dos documentos y es UN hecho: salía
        # "CH GSS → FA (2)", que se lee como dos cheques. Un depósito toca
        # ocho cheques y una cuenta y son ocho hechos, uno por cheque.
        if ev and ev.get("id_mov_doble"):
            g["hechos"].add(ev["id_mov_doble"])
            # 🚨 El tipo `retiro_socio_de_*` sirve para el retiro Y para el
            # aporte (lo distingue el signo), así que un grupo puede tener los
            # dos. Ahí ningún nombre es cierto para todos: se guarda el signo
            # de cada hecho y sólo se renombra si TODOS coinciden.
            try:
                g["signos"].add(float(ev.get("importe") or 0) < 0)
            except (TypeError, ValueError):
                g["signos"].add(None)
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
            g["cuantos"][q] = g["cuantos"].get(q, 0) + 1
    for g in grupos.values():
        # Lo que el grupo MOVIÓ, aunque no haya aportado: el lado más grande.
        g["bruto"] = max((abs(v) for v in g["por_col"].values()), default=0.0)
    _unir_las_dos_patas(grupos)
    _unir_anticipo_con_mercaderia(grupos, hasta)
    _unir_conversion_del_anticipo(grupos)
    # La venta se explica sola: el renglón de las facturas se lleva el margen.
    _nota = _nota_del_margen(venta)
    if _nota:
        vendido = next((g for g in grupos.values()
                        if g.get("col") == "facturas" and g["aporte"] > 0
                        and not g.get("fundido")), None)
        if vendido:
            vendido["nota"] = _nota
    out, menores = [], 0.0
    for g in sorted(grupos.values(),
                    key=lambda x: max(abs(x["aporte"]), x["bruto"]),
                    reverse=True):
        if g.get("fundido"):
            continue                           # ya salió con su otra pata
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
            from modules.historial.queries import label as _label

            # El signo sólo puede nombrar al grupo si el grupo tiene UN signo.
            _imp = ev.get("importe") if len(g.get("signos") or ()) == 1 else None
            g["regla"] = _label(ev["tipo"], _imp)
            md = ev.get("meta") or {}
            quienes = sorted(g["quienes"], key=lambda k: abs(g["quienes"][k]),
                             reverse=True)
            cerrado = False        # el texto ya cuenta cuántos son
            # La contraparte que sabe el evento manda sobre la que se adivina
            # de la etiqueta.
            quien = (md.get("codigo_prov") or (quienes[0] if quienes else ""))
            if len(g["hechos"]) > 1:
                # El rótulo sale de `corto()` salvo que el tipo se diga de otra
                # manera; SIN la contraparte, que acá son varias.
                rotulo, unidad = ROTULO_JUNTADO.get(
                    ev["tipo"], (_corto(ev["tipo"], "", _imp), ""))
                if unidad:
                    g["texto"] = f"{rotulo} · {len(g['hechos'])} {unidad}"
                else:
                    nombres = _nombres(g)
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
            elif ev["tipo"] in ("bap_anticipo_a_compra",
                                "bap_anticipo_a_compra_reverso"):
                # 🚨 TMT 2026-08-11: *"no sé qué es CP y ese número, quiero ver
                # el AI 16"*. Salía "AN AI → CP 10148": el código del proveedor
                # SIN el número de la importación, y un número que es el de la
                # compra. El nombre que usa la fábrica ya viene escrito en el
                # concepto (lo puso `convertir_a_compra` el 31/07, por este
                # mismo reclamo); acá sólo faltaba usarlo.
                nombre = _nombre_de_fabrica(ev.get("concepto") or "")
                _que = _texto_conversion(ev["tipo"], md.get("n_anticipos"))
                g["texto"] = f"{nombre} · {_que}" if nombre else _que
                cerrado = True
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
                g["texto"] = _corto(ev["tipo"], quien, _imp)
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
            g["url"] = _url_del_grupo(g, ev)

        elif g["n"] > 1:
            # ⭐ TMT 2026-08-06: *"decime algo de las facturas, de los abonos…
            # ¿clientes quizás?"*. "3 facturas nuevas" no dice nada; "3
            # facturas · AJT, SAC, GBC" dice de quién es la venta. Van los tres
            # que más pesan, que es lo que se mira.
            nombres = _nombres(g)
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
        if g.get("texto_unido"):
            g["texto"] = g["texto_unido"]
        # ⭐ El $/kg del hilado no cambia solo: cambia porque entró plata a las
        # compras del mes. Si la foto sabe por dónde entró (mig 0244), el
        # renglón lo dice ANTES de las dos cifras — "entraron al precio del
        # hilo los recargos de AC 39 y MD 1 (+6.849,51) · $/kg 3,0556 → 3,0591"
        # en vez de "cambió el $/kg de 3 etapas". Cuando la revaluación ya se
        # fundió con la llegada de la importación, la causa es esa llegada y
        # no hace falta otra. Tamara 05/09/2026.
        if (causa_tarifa and g.get("regla") == "Revaluación de stock"
                and not g.get("texto_unido")):
            g["texto"] = f"{causa_tarifa} · {g.get('texto') or ''}".strip(" ·")
        g["texto"], _causa = _quimicos(_abreviar_etapas(g.get("texto") or ""),
                                       g.get("aporte") or 0.0)
        # La nota que ya traía el grupo (el $/kg del anticipo, el margen de la
        # venta) manda: la causa de químicos sólo llena una nota vacía.
        if _causa and not g.get("nota"):
            g["nota"] = _causa
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
    # Los kg y los dólares vendidos en la ventana salen de la foto misma
    # (`venta_kg` / `venta_us` son acumulados), y el costo del $/kg de
    # terminado de la foto nueva (mig 0185). Sin las dos fotos no hay Δ.
    _ant = fila.get("anterior") or {}
    _venta = {"kg": _f({"a": fila.get("venta_kg")}, "a") or 0, "us": 0.0,
              "ukg": _f({"a": fila.get("terminado_ukg")}, "a") or 0}
    if _ant:
        _venta["kg"] = round((_f({"a": fila.get("venta_kg")}, "a") or 0)
                             - (_f({"a": _ant.get("venta_kg")}, "a") or 0), 2)
        _venta["us"] = round((_f({"a": fila.get("venta_us")}, "a") or 0)
                             - (_f({"a": _ant.get("venta_us")}, "a") or 0), 2)
    else:
        _venta = None
    fila["resumen"] = resumir(
        movs, None if fila["sin_registro"] else fila.get("d_utilidad"), idx,
        hasta=_hasta, venta=_venta, causa_tarifa=causa_tarifa(fila, _ant))
    fila["d_kg"] = fila.get("d_kg") or {}
    if fila.get("d_ukg") is None:
        fila["d_ukg"] = _d_ukg(fila, _ant)
    return fila
