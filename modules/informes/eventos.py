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
        r["docs"] = [d for d in docs if d]
        r["meta"] = md if isinstance(md, dict) else {}
        # Un batch agrupa el hecho entero ("3 anticipos → compra N° 10130");
        # sin batch, el hecho es el movimiento solo.
        r["grupo"] = r.get("batch_id") or f"m{r['id_mov_doble']}"
        out.append(r)
    return out


def indice(evs: list[dict]) -> dict[str, dict]:
    """`doc_id` de la foto → el evento que lo tocó.

    Si dos eventos tocan el mismo documento en la misma ventana gana el ÚLTIMO:
    es el que explica en qué estado quedó, que es lo que muestra el Δ.
    """
    idx: dict[str, dict] = {}
    for ev in evs or []:
        for d in ev.get("docs") or []:
            idx[d] = ev
    return idx
