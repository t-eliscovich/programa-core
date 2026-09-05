"""Los movimientos dobles como fuente de NOMBRES para la traza.

TMT 2026-08-07, mirando /historial: *"podés ver de traer algunas de acá… por
ejemplo anticipos e historial me gusta"*, *"cheque depositado en banco debería
ser un movimiento"*, *"cuando hay una devolución me gustaría que también
aparezca"*.

Tenía razón: la traza estaba reinventando —peor— algo que ya existe. El diff de
saldos sabe cuánta plata se movió pero no sabe QUÉ pasó, así que un cheque
depositado salía como dos hechos sueltos (una baja en Cheques y un alta en
Bancos) en vez de un movimiento con nombre. `scintela.mov_doble` tiene 20.405
filas que ya dicen exactamente eso, con su origen, su destino y su concepto.

**La división de trabajo:** `mov_doble` pone el NOMBRE y el AGRUPAMIENTO; el
diff de saldos pone la PLATA. El diff cierra al centavo y eso no se negocia —
los importes de `mov_doble` no siempre coinciden con el Δ del componente (una
retención, un redondeo, un cheque que cubre dos facturas). Acá sólo se lo usa
para saber que dos movimientos del diff son, en realidad, un mismo hecho.

⭐ Y sirve para atrás: `fecha_creacion` es `timestamptz` real, y la tabla es
NATIVA de Programa Core — no la tocan los TRUNCATE del sync del dBase que
arruinan `fecha_crea` en POSDAT, DOLARES, ACTIVOS y RETIROS. Por eso las
cobranzas y las devoluciones de la mañana sí se pueden fechar, aunque la
factura no haya sellado `fecha_modifica`.
"""
from __future__ import annotations

import logging

import db

_LOG = logging.getLogger("programa_core.eventos")

#: Tipos que NO se muestran en la traza. `retencion_doble_corregida` fue una
#: limpieza de una sola vez al principio (837 filas): mostrarla todos los días
#: es ensuciar la pantalla con algo que ya pasó y no va a volver a pasar.
#: TMT 2026-08-07: *"esto no me muestres, fue una corrección hecha al principio"*.
TIPOS_OCULTOS = {"retencion_doble_corregida", "reverso_retencion_doble_corregida"}

#: Tabla de `mov_doble` → prefijo del `doc_id` de la foto. Las que no están
#: (transacciones_bancarias, compra, xgast) no tienen fila propia en la foto:
#: el diff las ve dentro del saldo del banco o del componente, no como
#: documento.
PREFIJO = {
    "factura": "f", "cheque": "c", "caja": "k", "dolares": "d",
    "posdat": "p", "retiros": "r", "activos": "a",
}


def _doc(tabla: str | None, rid) -> str:
    p = PREFIJO.get((tabla or "").strip())
    return f"{p}{int(rid)}" if p and rid else ""


def de_la_ventana(desde, hasta) -> list[dict]:
    """Los movimientos dobles con `fecha_creacion` en (desde, hasta].

    Fail-soft: sin eventos la pantalla sigue andando, sólo con nombres más
    pobres. Nunca puede tumbar el detalle.
    """
    if not desde or not hasta:
        return []
    try:
        from modules.historial.queries import label as _label
    except Exception:  # noqa: BLE001
        def _label(tipo, importe=None):
            return (tipo or "").replace("_", " ")
    try:
        filas = db.fetch_all(
            """
            SELECT id_mov_doble, batch_id::text AS batch_id, tipo, metadata,
                   origen_table, origen_id, destino_table, destino_id,
                   importe, concepto, usuario, estado,
                   (fecha_creacion AT TIME ZONE 'America/Guayaquil')::date AS dia
              FROM scintela.mov_doble
             WHERE fecha_creacion >  %s
               AND fecha_creacion <= %s
             ORDER BY id_mov_doble
            """, (desde, hasta)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("eventos: no pude leer mov_doble (%s)", e)
        return []
    out = []
    for r in filas:
        if (r.get("tipo") or "") in TIPOS_OCULTOS:
            continue
        r = dict(r)
        # El signo puede dar vuelta el nombre: un retiro NEGATIVO es un aporte.
        r["label"] = _label(r.get("tipo") or "", r.get("importe"))
        docs = [_doc(r.get("origen_table"), r.get("origen_id")),
                _doc(r.get("destino_table"), r.get("destino_id"))]
        # 🚨 Una conversión de anticipos registra UNA sola `mov_doble` con
        # `origen_id` = el PRIMER anticipo ("representativo") y sin batch_id;
        # los otros N−1 viven sólo en `metadata.ids_anticipos`. Sin esto, de
        # tres anticipos uno matcheaba el evento y dos caían al camino sin
        # nombre: el mismo hecho salía en dos renglones.
        md = r.get("metadata") or {}
        if isinstance(md, dict):
            for i in (md.get("ids_anticipos") or []):
                docs.append(_doc("dolares", i))
            # 🚨 Lo mismo con el TOTALIZAR y su reverso: tocan N facturas y
            # registran UNA sola `mov_doble` que nombra la PRIMERA y la
            # ÚLTIMA. Las otras N−2 caían al camino sin nombre y la traza las
            # clasificaba por la FORMA del cambio — y como volver de 'T' a 'Z'
            # deja el saldo igual al importe, once facturas del reverso de MLZ
            # salieron rotuladas "facturas nuevas": ninguna se había emitido.
            # La metadata las tiene todas. TMT 2026-08-07 (dueña: *"¿por qué
            # una factura de MLZ me quedó en otro renglón?"*).
            for f in (md.get("antes") or []):
                if isinstance(f, dict) and f.get("id"):
                    docs.append(_doc("factura", f["id"]))
            # El reverso no repite el snapshot entero: guarda sólo los ids.
            for i in (md.get("ids_facturas") or []):
                docs.append(_doc("factura", i))
        r["docs"] = [d for d in docs if d]
        r["meta"] = md if isinstance(md, dict) else {}
        # Un batch agrupa el hecho entero ("3 anticipos → compra N° 10130");
        # sin batch, el hecho es el movimiento solo… salvo que la metadata
        # delate que varios movimientos son UNA sola operación.
        r["grupo"] = (r.get("batch_id") or _grupo_por_meta(r)
                      or f"m{r['id_mov_doble']}")
        out.append(r)
    _facturas_del_deshecho(out)
    return out


def _facturas_del_deshecho(evs: list[dict]) -> None:
    """El reverso del totalizar hereda las facturas del movimiento que deshizo.

    🚨 El totalizar guarda el snapshot `antes` con las N facturas; su REVERSO
    guarda sólo los contadores y un `id_mov_deshecho`. Sin resolverlo, el
    renglón decía "↩ FA MLZ totalizar · 29 facturas" y al lado seguían
    apareciendo "11 facturas nuevas · MLZ" y "FA #174039 MLZ" — las mismas
    facturas, contadas dos veces y con un nombre que además miente (ninguna se
    emitió: volver de 'T' a 'Z' deja el saldo igual al importe).
    """
    pendientes = {int(e["meta"]["id_mov_deshecho"]): e for e in evs
                  if (e.get("meta") or {}).get("id_mov_deshecho")}
    if not pendientes:
        return
    try:
        filas = db.fetch_all(
            "SELECT id_mov_doble, metadata FROM scintela.mov_doble "
            " WHERE id_mov_doble = ANY(%s)", (list(pendientes),)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("eventos: no pude leer el mov deshecho (%s)", e)
        return
    for r in filas:
        md = r.get("metadata") or {}
        if not isinstance(md, dict):
            continue
        destino = pendientes.get(int(r["id_mov_doble"]))
        if not destino:
            continue
        for f in (md.get("antes") or []):
            if isinstance(f, dict) and f.get("id"):
                d = _doc("factura", f["id"])
                if d and d not in destino["docs"]:
                    destino["docs"].append(d)


#: Los tipos donde N movimientos son UNA operación aunque no compartan
#: `batch_id`, y por qué campo de la metadata se reconocen.
#:
#: 🚨 El depósito consolidado escribe una `mov_doble` POR CHEQUE (ocho cheques
#: al banco = ocho filas) y no pasa `batch_id`, así que la traza los mostraba
#: como ocho renglones de una ida sola al banco. Comparten `id_transaccion`:
#: es literalmente el mismo depósito en la cuenta.
GRUPO_POR_META = {"cheque_depositado": "id_transaccion",
                  "reverso_cheque_depositado": "id_transaccion"}


def _grupo_por_meta(r: dict) -> str:
    campo = GRUPO_POR_META.get((r.get("tipo") or "").strip())
    valor = (r.get("meta") or {}).get(campo) if campo else None
    return f"{campo}:{valor}" if valor else ""


def _varios(conceptos: list[str], n: int) -> str:
    """El renglón de una cuenta con VARIOS movimientos.

    🚨 TMT 2026-08-07: *"banco no sé qué movimiento es todavía"*. Decía "BC · 3
    movimientos" y los conceptos —que son lo único que identifica al
    movimiento— vivían en el tooltip, donde no se ven.

    🚨 Y después: once movimientos daban *"BC · Anticipo USD AI $ 314.96 (ND
    Pichincha), Anticipo USD AI $ 251.75 (ND Pichincha), Anticipo USD AI
    $ 304.20 (ND Pichincha) +8"* — el mismo concepto tres veces, con el importe
    adentro. Los que empiezan igual se dicen UNA vez y se cuentan; los sueltos
    van tal cual. El primer intento resumía sólo si TODOS compartían prefijo, y
    en la ventana real había ocho anticipos y tres gastos de CC: no compartían
    nada y no resumía nada.
    """
    # 🚨 Tamara 05/09: tres anticipos iguales ("ANTICIPO AC 36" ×3) salían
    # como UNO, y "ANTICIPO MD" + "ANTICIPO MD DEL SALTO" como "2 × ANTICIPO"
    # (sin el MD). Los repetidos se CUENTAN, no se deduplican; y el prefijo
    # común respeta la palabra entera (ver `_prefijo_comun`).
    cuenta: dict[str, int] = {}
    for c in conceptos:
        if c:
            cuenta[c] = cuenta.get(c, 0) + 1
    vistos = list(cuenta)
    if not vistos:
        return f"BC · {n} movimientos"
    grupos: dict[str, list[str]] = {}
    for c in vistos:
        grupos.setdefault(" ".join(c.split()[:2]).upper(), []).append(c)
    partes = []
    for miembros in grupos.values():
        total = sum(cuenta[c] for c in miembros)
        if len(miembros) > 1:
            rot = _prefijo_comun(miembros) or " ".join(miembros[0].split()[:2])
            partes.append((total, f"{total} × {rot}"))
        elif total > 1:
            partes.append((total, f"{total} × {miembros[0]}"))
        else:
            partes.append((1, miembros[0]))
    # El grupo más grande primero — es la regla de toda la pantalla — y a
    # igualdad de tamaño, el texto más corto: entran más antes del "+N".
    partes = [p for _n, p in sorted(partes, key=lambda x: (-x[0], len(x[1])))]
    texto, sobran = "BC · ", 0
    for k, parte in enumerate(partes):
        if len(texto) + len(parte) > ANCHO_RENGLON and k:
            sobran = len(partes) - k
            break
        texto += ("" if texto.endswith("· ") else ", ") + parte
    return texto + (f" +{sobran}" if sobran else "")


#: Hasta dónde se estira el renglón de la cuenta antes de cortar con "+N". La
#: columna del concepto se lleva tres columnas y dobla, así que entra bastante
#: — pero un renglón de cuatro líneas ya es un párrafo.
ANCHO_RENGLON = 62


#: Con menos de esto el "prefijo común" es una coincidencia ("GS ", "CC ") y
#: resumir por ahí junta cosas que no tienen nada que ver.
MIN_PREFIJO = 8


def _prefijo_comun(textos: list[str]) -> str:
    """La parte inicial que comparten TODOS, cortada en palabra entera."""
    if len(textos) < 2:
        return ""
    comun = textos[0]
    for t in textos[1:]:
        i = 0
        while i < min(len(comun), len(t)) and comun[i] == t[i]:
            i += 1
        comun = comun[:i]
        if len(comun) < MIN_PREFIJO:
            return ""
    # Si el prefijo termina justo donde termina una palabra en TODOS los
    # textos ("ANTICIPO MD" vs "ANTICIPO MD DEL SALTO"), es una palabra
    # entera y se queda; si cortó una palabra al medio, se saca esa palabra.
    entera = all(len(t) == len(comun) or t[len(comun)] == " " for t in textos)
    if not entera:
        comun = comun.rsplit(" ", 1)[0] if " " in comun else comun
    comun = comun.strip(" ,·-$(")
    return comun if len(comun) >= MIN_PREFIJO else ""


def _id_bancario(ev: dict):
    """El `id_transaccion` del lado banco del movimiento."""
    for lado in ("destino", "origen"):
        if ev.get(f"{lado}_table") == "transacciones_bancarias":
            try:
                return int(ev.get(f"{lado}_id"))
            except (TypeError, ValueError):
                return None
    return None


def _no_banco_de_la_transaccion(evs: list[dict]) -> dict[int, int]:
    """`id_transaccion` → `no_banco`, para los hechos que no lo traen encima.

    🚨 TMT 2026-08-20, sobre el aporte de 128.625 de LAFER: el renglón del banco
    decía "Cheque emitido → otro · 102.973" y ese cheque era de 25.651,68. Los
    otros 128.625 eran el depósito del aporte, que en la misma ventana movió la
    MISMA cuenta — pero `capital.retirar()` y `capital.aportar()` guardan
    `metadata` sin `no_banco`, así que acá quedaban invisibles y la cuenta
    parecía tener un solo hecho: el Δ entero se le colgaba al que quedó, con un
    nombre FALSO. El `no_banco` que la metadata no trae lo sabe la transacción.

    Fail-soft: si la consulta falla se sigue como antes (nombres más pobres,
    nunca una pantalla caída).
    """
    faltan = sorted({i for ev in evs
                     if (ev.get("meta") or {}).get("no_banco") is None
                     and (i := _id_bancario(ev)) is not None})
    if not faltan:
        return {}
    try:
        filas = db.fetch_all(
            "SELECT id_transaccion, no_banco FROM scintela.transacciones_bancarias "
            " WHERE id_transaccion = ANY(%s)", (faltan,)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("eventos: no pude resolver el no_banco (%s)", e)
        return {}
    return {int(f["id_transaccion"]): int(f["no_banco"]) for f in filas
            if f.get("no_banco") is not None}


def _cuentas(evs: list[dict]) -> dict[str, dict]:
    """`b<no_banco>` → el hecho que movió esa cuenta en la ventana.

    🚨 TMT 2026-08-07, sobre un renglón que decía sólo *"Banco PICHINCHA
    −370"*: *"ahí tenemos un banco pichincha solo, no se está mostrando bien
    lo que queremos"*. Y tenía razón: la foto guarda los bancos por CUENTA y
    no por transacción (`saldo_bancos()` resuelve un running saldo), así que
    el "documento" del diff es la cuenta y su nombre es todo lo que se sabía.

    `mov_doble` sí sabe qué pasó, pero su lado banco es `transacciones_
    bancarias`, que no tiene prefijo en la foto — el puente es el `no_banco`,
    que sale de la metadata o, cuando el hecho no lo guardó, de la transacción
    misma (`_no_banco_de_la_transaccion`).

    ⭐ Cuando el único hecho de la cuenta es también el del otro lado (un
    depósito de cheques, una nota de débito contra un posdat), se devuelve EL
    MISMO evento: los dos lados caen en el mismo grupo y salen en un renglón
    que netea cero, que es lo que un traspaso es. Si la cuenta tuvo varios
    hechos distintos no se elige uno —sería atribuirle el Δ entero al que
    quedó— y el renglón dice cuántos fueron.
    """
    bancarios = [ev for ev in evs or []
                 if "transacciones_bancarias" in (ev.get("origen_table"),
                                                  ev.get("destino_table"))]
    de_la_transaccion = _no_banco_de_la_transaccion(bancarios)
    por_cuenta: dict[str, list[dict]] = {}
    for ev in bancarios:
        nb = (ev.get("meta") or {}).get("no_banco")
        if nb is None:
            nb = de_la_transaccion.get(_id_bancario(ev))
        try:
            doc = f"b{int(nb)}"
        except (TypeError, ValueError):
            continue
        por_cuenta.setdefault(doc, []).append(ev)
    out: dict[str, dict] = {}
    for doc, lista in por_cuenta.items():
        grupos = {e.get("grupo") for e in lista}
        if len(grupos) == 1:
            out[doc] = lista[0]
            continue
        conceptos = [(e.get("concepto") or "").strip() for e in lista]
        conceptos = [c for c in dict.fromkeys(conceptos) if c]
        out[doc] = {"tipo": "banco_varios", "grupo": doc, "docs": [doc],
                    "label": f"{len(grupos)} movimientos de la cuenta",
                    "texto": _varios(conceptos, len(grupos)),
                    "concepto": " · ".join(conceptos),
                    "meta": {}, "dia": lista[0].get("dia")}
    return out


#: El código de documento de `transacciones_bancarias`, en castellano. Es el
#: mismo vocabulario de `TIPOS_LABEL` para los `banco_*_directo`.
DOCUMENTO_BANCO = {
    "DE": "depósito", "CH": "cheque emitido", "TR": "transferencia",
    "ND": "nota de débito", "NC": "nota de crédito", "AC": "acreditación",
}


def transacciones(desde, hasta) -> dict[str, dict]:
    """`b<no_banco>` → lo que se cargó en esa cuenta, salga o no de `mov_doble`.

    🚨 TMT 2026-08-07, sobre un renglón que decía sólo *"Banco PICHINCHA
    759"*: *"fijate si identificás"*. `_cuentas()` sólo alcanza a los
    movimientos que dejaron `mov_doble` con `no_banco`; los que se cargan
    derecho por la pantalla de Bancos no dejan ninguno, y ésos son justo los
    que quedaban sin nombre. Acá se lee la tabla de transacciones por su
    `fecha_crea` —el momento en que se CARGÓ, no la fecha del movimiento, que
    puede ser vieja— y el concepto tipeado se usa como renglón.

    Fail-soft: sin esto el renglón vuelve a decir el nombre del banco y nada
    más, que es como estaba.
    """
    if not desde or not hasta:
        return {}
    # 🚨 TMT 2026-08-26 — el día lleva DOS conversiones, y las dos hacen falta.
    # `transacciones_bancarias.fecha_crea` es `timestamp` SIN zona (la tabla
    # viene del dBase) y guarda hora UTC. Con una sola conversión Postgres hace
    # lo contrario de lo que parece: LEE el valor como si ya fuera hora de
    # Guayaquil y devuelve un timestamptz que, al castear a date, se mira en la
    # zona de la sesión (UTC) — o sea que SUMA 5 horas en vez de restarlas.
    # Medido: una nota de débito cargada 15:26 de Ecuador daba día 27/08, y el
    # "ver" de la traza mandaba al Historial de un día sin movimientos. Todo lo
    # cargado después de las 14:00 se iba al día siguiente, por eso el link
    # andaba a la mañana y no a la tarde.
    #
    # ⭐ La distinción importa: en la consulta de arriba, sobre una columna que
    # SÍ tiene zona, la conversión simple es la CORRECTA. Va por el TIPO de la
    # columna, no por costumbre.
    #
    # Y el comentario vive acá, en Python, y no adentro del SQL: el test de la
    # nota diaria elige qué devolver mirando el TEXTO de la consulta, así que
    # una palabra de más en un comentario le cambia la respuesta.
    try:
        filas = db.fetch_all(
            """
            SELECT no_banco, documento, concepto, importe,
                   ((fecha_crea AT TIME ZONE 'UTC')
                        AT TIME ZONE 'America/Guayaquil')::date AS dia
              FROM scintela.transacciones_bancarias
             WHERE fecha_crea >  %s
               AND fecha_crea <= %s
               AND no_banco IS NOT NULL
             ORDER BY id_transaccion
            """, (desde, hasta)) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("eventos: no pude leer transacciones_bancarias (%s)", e)
        return {}
    por_cuenta: dict[str, list[dict]] = {}
    for r in filas:
        por_cuenta.setdefault(f"b{int(r['no_banco'])}", []).append(r)
    out: dict[str, dict] = {}
    for doc, lista in por_cuenta.items():
        conceptos = [(r.get("concepto") or "").strip() for r in lista]
        conceptos = [c for c in conceptos if c]
        if len(lista) == 1 and conceptos:
            # 🚨 El concepto solo puede ser cualquier cosa: el depósito de un
            # cheque se carga como *"1 ch.KRH"*, que no se entiende sin saber
            # que es un depósito. El tipo de documento adelante lo ubica.
            que = DOCUMENTO_BANCO.get(
                (lista[0].get("documento") or "").strip().upper()) or ""
            # …salvo que el concepto ya lo diga: "transferencia ·
            # TRANSFERENCIA RECIBIDA JVL" es la misma palabra dos veces.
            if que and que.split()[0] in conceptos[0].lower():
                que = ""
            texto = " · ".join(x for x in (que, conceptos[0]) if x)
        else:
            texto = _varios(conceptos, len(lista))
        # El ancho lo controla `_varios`: cortar acá dejaba el renglón partido
        # al medio de una palabra y sin el "+N" que avisa que falta algo.
        # ⭐ TMT 2026-08-12: a dónde lleva el renglón al clickearlo. "banco_cargado"
        # es una etiqueta INTERNA de la traza, no un tipo de `mov_doble`, así que
        # filtrar el historial por ella daba "Sin movimientos en este filtro".
        # El historial sí conoce el movimiento por su DOCUMENTO
        # (`banco_de_directo`, `banco_nd_directo`, …), que es lo que se manda.
        _doc_banco = (lista[0].get("documento") or "").strip().lower()
        out[doc] = {"tipo": "banco_cargado", "grupo": doc, "docs": [doc],
                    "tipo_historial": (f"banco_{_doc_banco}_directo"
                                       if _doc_banco else None),
                    "label": "Movimiento de banco", "texto": texto,
                    "concepto": " · ".join(dict.fromkeys(conceptos)),
                    "meta": {}, "dia": lista[0].get("dia")}
    return out


def indice(evs: list[dict], cuentas: dict | None = None) -> dict[str, dict]:
    """`doc_id` de la foto → el evento que lo tocó.

    Si dos eventos tocan el mismo documento en la misma ventana gana el ÚLTIMO:
    es el que explica en qué estado quedó, que es lo que muestra el Δ.
    """
    idx: dict[str, dict] = {}
    for ev in evs or []:
        for d in ev.get("docs") or []:
            idx[d] = ev
    # Primero lo que se cargó en la cuenta (alcanza a TODO), después lo que
    # `mov_doble` sabe (que además une el banco con el otro lado del hecho).
    idx.update(cuentas or {})
    for doc, hecho in _cuentas(evs).items():
        # 🚨 TMT 2026-08-07: *"acá me falta el número de AC 15 o AI 15"*.
        # Cuando la cuenta tuvo VARIOS hechos distintos, el renglón se arma con
        # los conceptos — y el del `mov_doble` de un anticipo dice "Anticipo
        # USD AC $ 1256.29", con el importe en el lugar del número. La FILA
        # BANCARIA sí guarda lo que la persona escribió ("ANTICIPO AC 15"), así
        # que para ese caso mandan los conceptos de la tabla de transacciones.
        # Cuando el hecho es UNO solo no se toca: ahí el nombre del hecho vale
        # más que el concepto ("CH → BC" en vez de "1 ch.KRH").
        base = (cuentas or {}).get(doc)
        if hecho.get("tipo") == "banco_varios" and base:
            hecho = dict(hecho, texto=base.get("texto") or hecho["texto"],
                         concepto=base.get("concepto") or hecho.get("concepto"))
        idx[doc] = hecho
    return idx
