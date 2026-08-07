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
        from modules.historial.queries import TIPOS_LABEL
    except Exception:  # noqa: BLE001
        TIPOS_LABEL = {}
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
        r["label"] = TIPOS_LABEL.get(r.get("tipo"), (r.get("tipo") or "").replace("_", " "))
        r["label"] = r["label"]
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

    🚨 TMT 2026-08-07: *"banco no sé qué movimiento es todavía"*. Decía
    "BC · 3 movimientos" y los conceptos —que son lo único que identifica al
    movimiento— vivían en el tooltip, donde no se ven. Van en el renglón: si
    no entran, se cortan con "+N", que sigue siendo más que un número pelado.
    """
    vistos = [c for c in dict.fromkeys(conceptos) if c]
    if not vistos:
        return f"BC · {n} movimientos"
    texto = "BC · " + ", ".join(vistos[:3])
    if len(vistos) > 3:
        texto += f" +{len(vistos) - 3}"
    return texto


def _cuentas(evs: list[dict]) -> dict[str, dict]:
    """`b<no_banco>` → el hecho que movió esa cuenta en la ventana.

    🚨 TMT 2026-08-07, sobre un renglón que decía sólo *"Banco PICHINCHA
    −370"*: *"ahí tenemos un banco pichincha solo, no se está mostrando bien
    lo que queremos"*. Y tenía razón: la foto guarda los bancos por CUENTA y
    no por transacción (`saldo_bancos()` resuelve un running saldo), así que
    el "documento" del diff es la cuenta y su nombre es todo lo que se sabía.

    `mov_doble` sí sabe qué pasó, pero su lado banco es `transacciones_
    bancarias`, que no tiene prefijo en la foto — el puente es el `no_banco`
    de la metadata.

    ⭐ Cuando el único hecho de la cuenta es también el del otro lado (un
    depósito de cheques, una nota de débito contra un posdat), se devuelve EL
    MISMO evento: los dos lados caen en el mismo grupo y salen en un renglón
    que netea cero, que es lo que un traspaso es. Si la cuenta tuvo varios
    hechos distintos no se elige uno —sería atribuirle el Δ entero al que
    quedó— y el renglón dice cuántos fueron.
    """
    por_cuenta: dict[str, list[dict]] = {}
    for ev in evs or []:
        if "transacciones_bancarias" not in (ev.get("origen_table"),
                                             ev.get("destino_table")):
            continue
        nb = (ev.get("meta") or {}).get("no_banco")
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
    try:
        filas = db.fetch_all(
            """
            SELECT no_banco, documento, concepto, importe,
                   (fecha_crea AT TIME ZONE 'America/Guayaquil')::date AS dia
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
        out[doc] = {"tipo": "banco_cargado", "grupo": doc, "docs": [doc],
                    "label": "Movimiento de banco", "texto": texto[:60],
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
    idx.update(_cuentas(evs))
    return idx
