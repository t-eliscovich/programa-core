"""Concepto de un cobro que NO se aplica a facturas — para la impresión diaria.

TMT 2026-08-04. Alex, por WhatsApp, con la tirilla del resumen del día en la
mano:

    "en el caso de MTM los dos fueron abonos a factura, pero refleja sin
     aplicar facturas … CEM corresponde a anticipos para pago de un cheque …
     JOH igualmente anticipo para pago de cheque … podemos colocar de forma
     manual un concepto cuando sea anticipo, o tener preestablecido: Abono a
     cheques, anticipo a facturas … es más porque esto se entrega a
     contabilidad y no van a saber qué hacer con el 'sin aplicar facturas'"

El resumen (`resumen_cobranza_dia` + `templates/cheques/resumen_dia.html`)
imprimía `— sin aplicar a facturas` para todo cobro sin filas en
`chequesxfact`. Este módulo resuelve las dos mitades del problema:

1. **El rótulo AUTOMÁTICO** (`etiquetas_automaticas`): en los 3 casos que marcó
   Alex el sistema YA sabía qué era cada cobro — el dato estaba en
   `mov_doble` y la impresión no lo leía.

   - **MTM** ($300 + $300): Alex los **aplicó** a la factura 177617 a las
     12:08 y 12:09… y a las **12:25 corrió TOTALIZAR ESTADO DE CUENTA de
     MTM**. `totalizar_estado_cuenta_ejecutar` **borra a propósito** los
     vínculos (`DELETE FROM scintela.chequesxfact WHERE id_fact = ANY(...)`,
     `n_links_borrados: 6`): el abono se redistribuye y el link 1-a-1 ya no
     sería cierto. La plata quedó; lo que se perdió fue el rastro — y el
     resumen lee justo esa tabla. **La dueña tenía razón: "MTM sí se aplicó
     y quedaron abonadas".**

     No es anecdótico: los 11 `cheque_aplicado_a_factura` activos SIN fila en
     `chequesxfact` desde el 1/7 son EXACTAMENTE las 4 corridas de TOTALIZAR
     (MTM 03/08, ARD y LKA 21/07, OGA 16/07). Cero falsos positivos sobre
     3.296 aplicaciones.

   - **CEM** ($3.020,69 + $1.000): PC les creó el **espejo NB=98**
     (`cheque_anticipo_espejo`) → anticipo, saldo a favor.

   - **JOH** ($258 + $770 = $1.028): `cheque_cancelado_por_anticipo` dice
     textual *"CANCELADO por anticipo #101750 $1.028,00 — ch 99366 JOH 1→X"*
     → anticipo que pagó el cheque 99366. Literal lo que describió Alex.
     ⚠ El mov apunta a UN solo anticipo aunque el anticipo lo formen varias
     filas, así que la etiqueta se propaga por `batch_id` a los hermanos del
     alta (si no, el de $770 quedaba mudo).

2. **El concepto MANUAL** (`bootstrap_columna`, `PRESETS`): columna nueva
   `scintela.cheque.concepto`, que se tipea en el alta (/cheques/nuevo). Cubre
   lo que no tiene huella en `mov_doble` — desde el 1/7 son 7 cobros cargados
   por PC sin aplicar (los otros 240 "sin aplicar" son `dbf-import`: el import
   del dBase nunca trajo las aplicaciones).

Regla de precedencia en la impresión: **manda el concepto tipeado**; el rótulo
automático va como detalle secundario. Si no hay ninguno de los dos, recién ahí
`— sin aplicar a facturas`.

⚠ `scintela.cheque.observacion` NO sirve para esto: es varchar(200)
append-only con tags (`[E]`, `[X]`, `[REBOTE]`, `[ENDOSO]`, `[C]`) — es una
bitácora, no un campo. Por eso va columna propia.
"""

from __future__ import annotations

import logging

import db as _db

_LOG = logging.getLogger("programa_core.cheques.concepto")

# Largo máximo del concepto. Entra cómodo en la columna de la impresión.
MAX_LEN = 60

# Opciones preestablecidas que pidió Alex ("o tener preestablecido: Abono a
# cheques, anticipo a facturas"). Van como <datalist>: sugeridas pero no
# obligatorias — el operador puede escribir cualquier otra cosa.
PRESETS: tuple[str, ...] = (
    "Abono a cheques",
    "Anticipo a facturas",
    "Anticipo del cliente (saldo a favor)",
    "Devolución al cliente",
    "Pago de otro cliente",
    "Nota de crédito",
)

_BOOTSTRAP_SQL = (
    "ALTER TABLE scintela.cheque "
    f"ADD COLUMN IF NOT EXISTS concepto VARCHAR({MAX_LEN})"
)
_bootstrapped = False


def bootstrap_columna(conn=None) -> None:
    """Crea `scintela.cheque.concepto` si falta. Idempotente y fail-soft.

    El deploy NO corre migraciones (ver skill `programa-core`), así que la
    columna se bootstrapea en caliente — mismo patrón que
    `conciliacion/saldo_snapshot.py` y `bancos/apertura.py`.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL, conn=conn)
    except Exception as exc:  # noqa: BLE001 -- fail-soft: nunca tumbar el alta
        _LOG.exception("bootstrap cheque.concepto falló: %s", exc)
    _bootstrapped = True


def limpiar(valor) -> str | None:
    """Normaliza un concepto tipeado: trim, cap a MAX_LEN, '' → None."""
    txt = (valor or "").strip()
    if not txt:
        return None
    return txt[:MAX_LEN]


# --------------------------------------------------------------------------
# Rótulo automático desde mov_doble
# --------------------------------------------------------------------------

# Tipos de mov_doble que explican un cobro sin filas en chequesxfact.
#
# ⚠ Los tres caminos por los que un anticipo puede cancelar un cheque en
# cartera, verificados contra prod el 04/08 — hacen falta los tres:
#   · `cheque_cancelado_por_anticipo` con destino = el DEPÓSITO mismo
#     (JOH 03/08: el anticipo se consumió entero, no hubo sobrante ni espejo).
#   · `cheque_cancelado_por_anticipo` con destino = el ESPEJO NB=98
#     (CEM 03/08: hubo sobrante → espejo 101722, y el mov apunta al espejo).
#   · `anticipo_neteado`, que sí trae UNA fila POR espejo (101722 y 101724)
#     — es el que salva al segundo depósito de CEM, porque el
#     `cheque_cancelado_por_anticipo` referencia a uno solo.
# Y aún así el mov apunta a UN anticipo aunque el anticipo lo formen varias
# filas, así que la etiqueta se propaga por el `batch_id` del ALTA a los
# hermanos de la misma cobranza (sin eso, el JOH de $770 quedaba mudo).
_TIPOS = (
    "cheque_creado",
    "cheque_aplicado_a_factura",
    "cheque_anticipo_espejo",
    "cheque_cancelado_por_anticipo",
    "anticipo_neteado",
)


def _money_es(v) -> str:
    """Formato EC/EU: punto = miles, coma = decimales. '' si no es número."""
    try:
        s = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return ""
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _fecha_es(f) -> str:
    """dd/mm — corto, para meter en el rótulo sin comerse la columna."""
    try:
        return f"{f.day:02d}/{f.month:02d}"
    except AttributeError:
        return ""


def explicaciones(ids_cheque, codigos_por_id=None, conn=None) -> dict[int, dict]:
    """Qué fue cada cobro que quedó SIN filas vivas en `chequesxfact`.

    Devuelve `{id_cheque: {"texto": str, "origen": str, "facturas": [...]}}`
    SOLO para los que tienen una explicación. Los que no aparecen no tienen
    huella y caen al concepto manual (o al `— sin aplicar a facturas`).

    `origen` ∈ {'totalizar', 'anticipo_cheque', 'anticipo'} y `facturas` es la
    lista `[{'numf', 'importe'}]` del vínculo archivado — es lo que pidió la
    dueña el 04/08: *"aunque se totalice la cuenta quiero que en cobranza
    quede guardado qué factura se pagó"*.

    Sólo lectura. Fail-soft: ante cualquier error devuelve {} y la impresión
    se comporta exactamente como antes.
    """
    ids = sorted({int(i) for i in (ids_cheque or []) if i is not None})
    if not ids:
        return {}
    try:
        filas = _db.fetch_all(
            """
            SELECT tipo, origen_id, destino_id, importe, batch_id, metadata
              FROM scintela.mov_doble
             WHERE estado = 'activo'
               AND tipo = ANY(%s)
               AND ( (origen_table  = 'cheque' AND origen_id  = ANY(%s))
                  OR (destino_table = 'cheque' AND destino_id = ANY(%s)) )
             ORDER BY id_mov_doble
            """,
            (list(_TIPOS), ids, ids),
            conn=conn,
        ) or []
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        _LOG.exception("explicaciones falló: %s", exc)
        return {}

    ids_set = set(ids)
    batch_alta: dict[int, str] = {}      # id_cheque → batch_id de su alta
    es_anticipo: set[int] = set()
    aplicadas: dict[int, list[dict]] = {}
    espejo_de: dict[int, int] = {}       # id espejo NB=98 → id del depósito padre
    padres_con_espejo: set[int] = set()
    # id del anticipo (depósito O espejo) → id del cheque que canceló
    cancelo: dict[int, int] = {}

    for r in filas:
        tipo = r.get("tipo")
        meta = r.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        oid, did = r.get("origen_id"), r.get("destino_id")
        batch = r.get("batch_id")
        batch = str(batch) if batch else None

        if tipo == "cheque_creado" and oid in ids_set:
            if batch:
                batch_alta[oid] = batch
            if meta.get("es_anticipo"):
                es_anticipo.add(oid)
        elif tipo == "cheque_aplicado_a_factura" and oid in ids_set:
            aplicadas.setdefault(oid, []).append({
                "numf": meta.get("numf") or did,
                "id_fact": meta.get("id_factura") or did,
                "importe": r.get("importe"),
                "saldo_post": meta.get("saldo_factura_post"),
                "stat_post": meta.get("stat_factura_post"),
            })
        elif tipo == "cheque_anticipo_espejo" and oid in ids_set:
            padres_con_espejo.add(oid)
            if did:
                espejo_de[did] = oid
        elif tipo == "cheque_cancelado_por_anticipo":
            # origen = cheque CANCELADO · destino = el anticipo que lo pagó
            # (que puede ser el depósito mismo O su espejo NB=98).
            anticipo = meta.get("id_cheque_anticipo") or did
            if anticipo and oid:
                cancelo[int(anticipo)] = int(oid)
        elif tipo == "anticipo_neteado":
            # origen = el espejo del anticipo · destino = el cheque cancelado.
            if oid and did:
                cancelo[int(oid)] = int(did)

    # El anticipo referenciado puede ser el ESPEJO: traducirlo al depósito.
    cancelo_padre: dict[int, int] = {}
    for anticipo, cancelado in cancelo.items():
        cancelo_padre[espejo_de.get(anticipo, anticipo)] = cancelado
    # …y propagarlo a los hermanos del MISMO alta (el mov apunta a un solo
    # anticipo aunque el anticipo lo formen varias filas de la cobranza).
    por_batch: dict[str, int] = {
        batch_alta[cid]: cancelado
        for cid, cancelado in cancelo_padre.items()
        if cid in batch_alta
    }

    # Cómo se llama el cheque cancelado (una sola query).
    #
    # Regla del skill `programa-core`: la dueña nombra los cheques por
    # `no_cheque`, NUNCA por el id interno. Pero **1.430 de los 2.043 cheques
    # vivos no tienen `no_cheque`** (verificado en prod el 04/08 — los dos de
    # este caso, 99366 y 99741, entre ellos), así que "poné el número de
    # cheque o algo" (dueña, 04/08) necesita un fallback: banco + fecha de
    # depósito, que es como los identifica la pantalla de cartera.
    cache_ref: dict[int, tuple[str, object]] = {}
    ids_cancelados = sorted(set(cancelo_padre.values()) | set(por_batch.values()))
    if ids_cancelados:
        try:
            for row in (_db.fetch_all(
                "SELECT id_cheque, COALESCE(no_cheque, '') AS no_cheque, "
                "       COALESCE(banco, '') AS banco, fechad, importe "
                "  FROM scintela.cheque WHERE id_cheque = ANY(%s)",
                (ids_cancelados,), conn=conn,
            ) or []):
                num = (row.get("no_cheque") or "").strip()
                if not num:
                    banco = (row.get("banco") or "").strip().title()
                    cuando = _fecha_es(row.get("fechad"))
                    num = " ".join(x for x in (banco, f"dep. {cuando}" if cuando else "") if x)
                cache_ref[int(row["id_cheque"])] = (num, row.get("importe"))
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            _LOG.exception("lookup de cheques cancelados falló: %s", exc)

    # Fecha del TOTALIZAR por cliente — para que el rótulo diga CUÁNDO se
    # redistribuyó el vínculo ("…por TOTALIZAR del 03/08"), pedido de la
    # dueña el 04/08.
    fecha_totalizar = _fechas_totalizar(
        {(codigos_por_id or {}).get(c) for c in aplicadas} - {None}, conn=conn)

    # Importe de cada factura tocada — `mov_doble` guarda el numf y lo
    # aplicado pero no el total de la factura, y sin eso la columna "importe"
    # de la tirilla quedaba vacía. La dueña, 04/08: "mientras siga quedando
    # encolumnado y prolijo sí".
    ids_fact = sorted({a["id_fact"] for apps in aplicadas.values() for a in apps
                       if a.get("id_fact")})
    imp_fact: dict[int, object] = {}
    if ids_fact:
        try:
            for row in (_db.fetch_all(
                "SELECT id_factura, importe FROM scintela.factura "
                " WHERE id_factura = ANY(%s)", (ids_fact,), conn=conn,
            ) or []):
                imp_fact[int(row["id_factura"])] = row.get("importe")
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            _LOG.exception("lookup de importes de factura falló: %s", exc)
    for apps in aplicadas.values():
        for a in apps:
            a["fact_importe"] = imp_fact.get(a.pop("id_fact", None))

    out: dict[int, dict] = {}
    for cid in ids:
        # 1. Aplicado a facturas — el vínculo lo redistribuyó TOTALIZAR.
        if cid in aplicadas:
            apps = aplicadas[cid]
            refs = ", ".join(dict.fromkeys(str(a["numf"]) for a in apps))
            plural = "s" if len(refs.split(", ")) > 1 else ""
            cuando = fecha_totalizar.get((codigos_por_id or {}).get(cid))
            sufijo = f" del {_fecha_es(cuando)}" if cuando else ""
            # La dueña, 04/08, sobre un rótulo genérico que le mostré:
            # "esto no igual, me gustaría que me digas a qué factura fue
            # aplicada realmente". Así que el número de factura va PRIMERO y
            # el TOTALIZAR queda como aclaración de por qué el vínculo vivo
            # ya no está.
            out[cid] = {
                "origen": "totalizar",
                "texto": (
                    f"aplicado a la factura{plural} {refs}"
                    f" · el TOTALIZAR{sufijo} redistribuyó el abono"
                ),
                "facturas": apps,
            }
            continue
        # 2. Anticipo que canceló un cheque en cartera.
        cancelado = cancelo_padre.get(cid) or por_batch.get(batch_alta.get(cid, ""))
        if cancelado:
            ref, imp = cache_ref.get(cancelado, ("", None))
            monto = _money_es(imp)
            detalle = f" (${monto})" if monto else ""
            out[cid] = {
                "origen": "anticipo_cheque",
                "texto": (
                    f"anticipo · canceló el cheque {ref}{detalle}" if ref
                    else f"anticipo · canceló un cheque en cartera{detalle}"
                ),
                "facturas": [],
                "id_cheque_cancelado": cancelado,
            }
            continue
        # 3. Anticipo suelto (espejo NB=98 / marcado como anticipo al alta).
        if cid in padres_con_espejo or cid in es_anticipo:
            out[cid] = {
                "origen": "anticipo",
                "texto": "anticipo · saldo a favor del cliente",
                "facturas": [],
            }
    return out


def _fechas_totalizar(codigos, conn=None) -> dict:
    """`{codigo_cli: fecha del último TOTALIZAR}`. {} si no hay o falla.

    El evento vive en `mov_doble` con `tipo='totalizar_estado_cuenta'` y el
    cliente adentro de `metadata->>'codigo_cli'` (no hay columna propia).
    """
    codigos = sorted({(c or "").strip().upper() for c in (codigos or []) if c})
    if not codigos:
        return {}
    try:
        filas = _db.fetch_all(
            """
            SELECT metadata ->> 'codigo_cli' AS codigo_cli,
                   MAX(fecha_creacion)       AS cuando
              FROM scintela.mov_doble
             WHERE tipo = 'totalizar_estado_cuenta'
               AND estado = 'activo'
               AND metadata ->> 'codigo_cli' = ANY(%s)
             GROUP BY 1
            """,
            (codigos,), conn=conn,
        ) or []
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        _LOG.exception("_fechas_totalizar falló: %s", exc)
        return {}
    return {r["codigo_cli"]: r["cuando"] for r in filas if r.get("cuando")}


def etiquetas_automaticas(ids_cheque, codigos_por_id=None, conn=None) -> dict[int, str]:
    """Igual que `explicaciones` pero devolviendo sólo el texto."""
    return {
        k: v["texto"]
        for k, v in explicaciones(ids_cheque, codigos_por_id, conn=conn).items()
    }
