"""Compras LOCALES de hilo — cruce contra el programa y carga automática.

TMT 2026-07-30 (dueña): *"la fábrica importa hilo pero también hace compras
locales. En Ingreso de hilado hay que sumarlas, la columna se va a llamar
IMPORTACIÓN/COMPRA"*.

## El modelo (por qué NO es una importación)

Una importación se paga por ANTICIPADO (anticipos USD en `scintela.dolares`) y
recién cuando Asinfo la marca recibida el BAP automático la convierte en compra.
Una compra LOCAL no tiene anticipo: llega la mercadería con la factura del
proveedor y **nace el pasivo** — igual que tejeduría. Por eso:

  * el disparador es la RECEPCIÓN (dueña: *"una vez que llegan se genera un
    pasivo"*), y la compra se fecha con la fecha de recepción;

    TMT 2026-09-16 — hasta hoy eso era la INTENCIÓN pero no el mecanismo: la
    consulta de Asinfo arrancaba de `factura_proveedor`, así que el motor no
    veía la entrega hasta que alguien cargaba la factura. Medido sobre 15
    recepciones (`/admin/debug-hilo-local`): entre el BOD y su factura pasaron
    **~21 horas de mediana y hasta 5 días**, y en toda esa ventana el hilo ya
    estaba en el stock del programa sin su pasivo — la utilidad del mes alta
    por el valor de lo que entró. Ahora la consulta parte de la RECEPCIÓN, que
    es autosuficiente: `id_empresa` y `numero_factura` son columnas propias y
    NOT NULL en Asinfo, así que la compra nace con su número de factura puesto;
  * el importe es el de la FACTURA del proveedor en Asinfo, con IVA (desde
    el 17/09/2026; antes un tarifario, ver guarda 5);
  * la compra se crea impaga → `compras.queries.crear` le arma su `posdat`
    banc=0, que ES el pasivo, con vencimiento = fecha + proveedor.plazo
    (HY 60 días, EP 2 días — lo mismo que hace el FoxPro).

## Las guardas (fail-closed, en este orden)

 1. **Asinfo mudo → no se carga nada.** Si el bridge devuelve [], salimos.
 2. **Sólo recibidas** (con fecha de recepción y kg > 0 en la bodega 51).
 3. **Fecha de corte** — no se cargan recepciones anteriores a `CORTE`. Sin
    esto, prender el automático arrastraría un año de facturas históricas que
    ya están cargadas a mano o archivadas en el FoxPro.
 4. **Proveedor mapeado por RUC** o se saltea (nunca adivinamos el código).
 5. **La factura está en Asinfo** o se espera — nunca inventamos un precio.
    El importe es `factura_proveedor.total + impuesto` (con IVA, al centavo),
    prorrateado por kilos si la entrega es parcial. Hasta el 17/09/2026 la
    plata salía de un TARIFARIO $/kg (una tarifa por proveedor, decisión del
    30/07) y estaba MAL: HY vende hilos de 2,30 a 3,80 $/kg y con 2,645 fijo
    las 13 compras del motor quedaron $ 18.772 abajo de sus facturas. Tamara:
    *"para adelante sea sólo de Asinfo el total de la factura y no más
    tarifas"*. El costo es esperar la factura (~1 día, hasta 5): en esa
    ventana la campanita avisa, y cuando aparece la compra entra sola.
 6. **Ya cruzada** — si la recepción ya tiene una compra en PC, no se vuelve
    a cargar. Idempotente. El match va en DOS pasos, en este orden:
      a. por **BOD** (`compra.comprobante`) — la identidad del hecho. Es lo que
         permite que una factura entregada en dos tandas genere DOS pasivos:
         cruzando sólo por nº de factura, la segunda entrega se vería como "ya
         cargada" y su plata no entraría nunca.
      b. por **(proveedor, nº de factura)** — la red para lo que no tiene BOD:
         las compras tipeadas a mano y las históricas del dBase.
 7. **Topes por corrida** (cantidad e importe) — un cambio raro en Asinfo no
    puede generar 200 compras de golpe.
 8. **Switch de ambiente** `HILO_LOCAL_AUTO=0`.

El período cerrado lo valida `compras.queries.crear` (`asegurar_fecha_abierta`)
y se reporta como salteada, sin cortar el lote.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import date

import db
from filters import num_es
from modules.asinfo import service as asinfo_service
from modules.compras_locales import queries as _tar

_LOG = logging.getLogger("programa_core.compras_locales")

#: `usuario_crea` de las compras que crea este motor.
#:
#: ⚠ A DIFERENCIA de las de tejeduría y de las BAP, este marcador **NO** se
#: agrega a la lista de preservados de `scripts/import_dbf.py`, y es a
#: propósito: el FoxPro SÍ tipea estas compras (hoy casi todas las de HY en
#: /compras tienen origen "dBase"). Si las preserváramos, después de un sync
#: quedarían DUPLICADAS — la nuestra y la del DBF. Dejándolas caer, el sync las
#: reemplaza por la del FoxPro, que es la misma factura con el mismo importe.
#: Y si el FoxPro no la tipeó, la guarda 6 no encuentra cruce en la próxima
#: corrida y el motor la vuelve a cargar solo. Se cura sola en los dos sentidos.
MARCADOR_CARGA = "asinfo-hilo-local"

#: Las compras se graban **con IVA** (así lo hacía el dBase). Se usa para
#: volver a neto donde el flujo valúa el hilado sin IVA.
IVA = 1.15

#: No se cargan recepciones anteriores a esta fecha (guarda 3). Se eligió el
#: 01/07/2026 porque las compras locales previas ya entraron por el sync del
#: dBase o se tipearon a mano; arrancar antes duplicaría.
CORTE = date(2026, 7, 1)

#: Topes por corrida (guarda 7).
TOPE_COMPRAS = 20
TOPE_IMPORTE = 500_000.0

_AUTO_LOCK = threading.Lock()
_auto_ultimo_ts = 0.0
_AUTO_INTERVALO_MIN = 1800.0  # 30 min entre corridas de fondo


def _importe_asinfo(kg: float, total: float | None, iva: float | None,
                    kg_factura: float | None) -> float | None:
    """La plata de la FACTURA de Asinfo para esta entrega, o None si no está.

    `total` es el neto de la factura y `iva` su impuesto (columnas
    `factura_proveedor.total` / `.impuesto`, verificado: HY 37649 = 1.447,51
    + 217,13). Se suman tal cual —sin recalcular el 15%— así la compra queda
    al centavo con el papel del proveedor.

    Si la factura tiene más kilos que la entrega (llegó en tandas), se
    prorratea por kilos: cada BOD carga su parte y la suma cierra con la
    factura. Sin kilos de factura conocidos se toma la factura entera.
    """
    if kg <= 0 or not total or float(total) <= 0:
        return None
    bruto = float(total) + float(iva or 0)
    kf = float(kg_factura or 0)
    if kf > 0 and kg < kf - 0.005:
        bruto = bruto * kg / kf
    return round(bruto, 2)


# ---------------------------------------------------------------------------
# Cruce contra scintela.compra
# ---------------------------------------------------------------------------
def _buscar_compras(refs: set[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
    """{(codigo_prov, nº factura) → [compras]} para las refs pedidas.

    El nº de factura del proveedor vive en `compra.concepto`. **Es el PRIMER
    número del concepto, no todos sus dígitos**: el FoxPro escribe
    `CONCEPTO = LEFT(factura,13) + STR(DAY(FECHA),2)` (ALTAS.PRG L649), así que
    la factura 37231 cargada un día 8 queda como `'37231         8'`. Sacarle
    todos los no-dígitos daría 372318 y no matchearía nunca — por eso acá se usa
    `substring(... from '^(\\d+)')` y no el `regexp_replace` de las importaciones.

    Trae además el estado de pago, con la MISMA lógica que la columna "Pagada"
    de /compras: la fuente de verdad es el posdatado vinculado por `mov_doble`.
    `saldo_pasivo` = lo que todavía se debe (posdat abierto, banc=0).

    Una sola query. Fail-soft: {} si no hay refs o la DB falla.
    """
    if not refs:
        return {}
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            r"""
            SELECT c.id_compra,
                   UPPER(TRIM(c.codigo_prov)) AS codigo_prov,
                   NULLIF(substring(btrim(COALESCE(c.concepto, '')) FROM '^(\d+)'), '')::bigint
                       AS fact_num,
                   c.importe, c.kg, c.tipo, c.concepto, c.usuario_crea,
                   TO_CHAR(c.fecha, 'YYYY-MM-DD') AS fecha,
                   COALESCE((
                       SELECT SUM(pd.importe)
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ), 0) AS saldo_pasivo,
                   EXISTS (
                       SELECT 1
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ) AS tiene_posdat,
                   -- Sin vínculo mov_doble (histórica del dBase): el BANC
                   -- importado NO sirve — el sync del dBase está parado desde
                   -- el 10/07 y ese campo quedó congelado ANTES del pago. Manda
                   -- scintela.posdat, que se reconcilia aparte y sí está al
                   -- día: sin posdatado abierto que la represente = PAGADA.
                   -- MISMA expresión que la columna "Pagada" de /compras: dos
                   -- pantallas diciendo cosas distintas de la misma compra
                   -- sería peor que el bug.
                   ((c.no_banco IS NOT NULL AND c.no_banco <> 0)
                    OR (c.cuenta_pagada IS NOT NULL AND btrim(c.cuenta_pagada) <> '')
                    OR NOT EXISTS (
                          SELECT 1 FROM scintela.posdat pd
                           WHERE UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                             AND COALESCE(pd.banc, 0) = 0
                             AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                             -- Un posdatado de $0,00 no es una deuda.
                             AND ABS(COALESCE(pd.importe, 0)) > 0.01
                             -- Con concepto de los dos lados manda el concepto:
                             -- es lo único que distingue CUOTAS del mismo
                             -- importe y fecha (caso CN, 6 × 20.200 el mismo
                             -- día). El posdat del dBase lo escribe con relleno,
                             -- por eso se colapsan los espacios. Si alguno viene
                             -- vacío (caso HY) se cae a fecha + importe.
                             AND CASE
                                   WHEN btrim(COALESCE(pd.concepto, '')) <> ''
                                    AND btrim(COALESCE(c.concepto, ''))  <> ''
                                   THEN regexp_replace(upper(btrim(pd.concepto)), '[[:space:]]+', ' ', 'g')
                                      = regexp_replace(upper(btrim(c.concepto)),  '[[:space:]]+', ' ', 'g')
                                   ELSE pd.fecha = c.fecha
                                    AND ABS(COALESCE(pd.importe, 0) - COALESCE(c.importe, 0)) < 0.01
                                 END
                    ))
                       AS pagada_legacy
              FROM scintela.compra c
             WHERE UPPER(TRIM(c.codigo_prov)) = ANY(%s)
               AND COALESCE(c.stat, '') <> 'Y'
               AND NULLIF(substring(btrim(COALESCE(c.concepto, '')) FROM '^(\d+)'), '')::bigint
                   = ANY(%s)
            """,
            (provs, [int(n) for n in numeros]),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("compras_locales._buscar_compras falló: %s", e)
        return {}

    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows or []:
        prov = (r.get("codigo_prov") or "").strip().upper()
        fnum = r.get("fact_num")
        if not prov or fnum is None:
            continue
        out.setdefault((prov, int(fnum)), []).append(r)
    return out


def _buscar_por_bod(bods: set[str]) -> dict[str, list[dict]]:
    """{BOD → [compras]} — el cruce por el DOCUMENTO de la entrega.

    El BOD va en `compra.comprobante` desde 2026-09-16. Es la llave primaria
    del match: identifica la ENTREGA, que es el hecho que crea el pasivo. El
    cruce por nº de factura (`_buscar_compras`) queda como red para lo que no
    tiene BOD — lo tipeado a mano y lo que trajo el sync del dBase.

    Trae las mismas columnas de estado de pago que `_buscar_compras`, más los
    `kg` y el `concepto` que ya tiene cargados, que es lo que mira el ajuste
    cuando una entrega crece en Asinfo.

    Una sola query. Fail-soft: {} si no hay bods o la DB falla.
    """
    if not bods:
        return {}
    lista = sorted({b.strip().upper() for b in bods if (b or "").strip()})
    if not lista:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT c.id_compra,
                   UPPER(TRIM(COALESCE(c.comprobante, ''))) AS bod,
                   UPPER(TRIM(c.codigo_prov)) AS codigo_prov,
                   c.importe, c.kg, c.tipo, c.concepto, c.usuario_crea,
                   TO_CHAR(c.fecha, 'YYYY-MM-DD') AS fecha,
                   COALESCE((
                       SELECT SUM(pd.importe)
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ), 0) AS saldo_pasivo,
                   EXISTS (
                       SELECT 1
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ) AS tiene_posdat,
                   -- Una compra creada por este motor SIEMPRE nace con su
                   -- posdat, así que acá `pagada_legacy` no aplica: si no
                   -- tiene posdat viva es porque se pagó.
                   FALSE AS pagada_legacy
              FROM scintela.compra c
             WHERE UPPER(TRIM(COALESCE(c.comprobante, ''))) = ANY(%s)
               AND COALESCE(c.stat, '') <> 'Y'
            """,
            (lista,),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("compras_locales._buscar_por_bod falló: %s", e)
        return {}

    out: dict[str, list[dict]] = {}
    for r in rows or []:
        bod = (r.get("bod") or "").strip().upper()
        if bod:
            out.setdefault(bod, []).append(r)
    return out


def _estado_pago(hits: list[dict]) -> dict:
    """Estado de pago del conjunto de compras cruzadas contra una factura.

    - `pagada`  = todas saldadas.
    - `saldo`   = lo que todavía se debe (posdat abierto). 0 si está pagada.
    - `parcial` = hay saldo pero es MENOR que el importe → pago a medias, y ahí
      el saldo sí se muestra en pantalla (si no, el importe ya es el pasivo).
    """
    importe = sum(float(h.get("importe") or 0) for h in hits)
    saldo = 0.0
    pagadas = 0
    for h in hits:
        if h.get("tiene_posdat"):
            s = float(h.get("saldo_pasivo") or 0)
            saldo += s
            if abs(s) < 0.01:
                pagadas += 1
        elif h.get("pagada_legacy"):
            pagadas += 1
        else:
            # Histórica del dBase sin vínculo y sin banco → deuda viva.
            saldo += float(h.get("importe") or 0)
    pagada = pagadas == len(hits) and abs(saldo) < 0.01
    return {
        "importe": round(importe, 2),
        "pagada": pagada,
        "saldo": round(saldo, 2),
        "parcial": (not pagada) and abs(saldo) > 0.01
        and abs(saldo - importe) > 0.01,
    }


# ---------------------------------------------------------------------------
# Filas para la pantalla
# ---------------------------------------------------------------------------
def compras_locales_con_cruce(limite: int = 200) -> list[dict]:
    """Compras locales de Asinfo, normalizadas al shape de /importaciones.

    Cada fila trae las mismas claves que una importación (`im_numero`, `fecha`,
    `prov`, `codigo`, `nota`, `kg`, `recibida`, `fecha_recepcion`, `compra`,
    `fuente`, `importe_programa`) más las propias:

        origen        — siempre 'compra' (las importaciones traen 'importacion')
        producto      — código de producto de Asinfo
        fact_num      — nº de factura del proveedor (el que va al concepto)
        importe_sugerido — la factura de Asinfo (total + IVA, prorrateada por
                          kg si la entrega es parcial); None si todavía no está
        pagada / saldo / parcial — estado del pasivo

    Fail-soft en todos los tramos: [] si Asinfo no contesta.
    """
    try:
        crudas = asinfo_service.compras_locales_asinfo(limite=limite)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("compras_locales_asinfo falló: %s", e)
        return []
    if not crudas:
        return []

    por_ruc = _tar.proveedores_por_ruc()

    filas: list[dict] = []
    refs: set[tuple[str, int]] = set()
    for c in crudas:
        prov = por_ruc.get((c.get("ruc") or "").strip())
        fact = c.get("fact_num")
        if prov and fact is not None:
            refs.add((prov, int(fact)))
        kg = float(c.get("kg") or 0)
        importe = _importe_asinfo(kg, c.get("total_asinfo"), c.get("iva_asinfo"),
                                  c.get("kg_factura"))
        filas.append({
            "origen": "compra",
            "im_numero": c.get("fp_numero"),
            "fecha": c.get("fecha"),
            "fecha_recepcion": c.get("fecha_recepcion"),
            "fecha_recepcion_pc": None,
            "bod": c.get("bod"),
            "bod_creada": c.get("bod_creada"),
            "anulada": bool(c.get("anulada")),
            "recibida": bool(c.get("recibida")),
            "proveedor": c.get("proveedor"),
            "nota": c.get("nota"),
            "kg": kg,
            "kg_recibidos": None,
            "total_asinfo": c.get("total_asinfo"),
            "prov": prov,
            "numero": fact,
            "numero_hasta": None,
            "codigo": (f"{prov} {fact}" if prov and fact is not None else None),
            "producto": c.get("producto"),
            "n_productos": int(c.get("n_productos") or 0),
            "numero_factura": c.get("numero_factura"),
            "fact_num": fact,
            "importe_sugerido": importe,
            # Claves que la plantilla comparte con las importaciones.
            "compra": None,
            "anticipo": None,
            "anticipo_total": 0.0,
            "anticipo_usd_dolares": 0.0,
            "movimientos": [],
            "fuente": None,
            "importe_programa": None,
            "recibido_pc": bool(c.get("recibida")),
            "codigo_compartido": False,
            "pagada": False,
            "saldo": 0.0,
            "parcial": False,
            # Anterior a la fecha de corte: el motor no la va a tocar nunca, y
            # además `scintela.compra` ni siquiera la tiene (COMPRAS.DBF es
            # rotativo ~3 meses, así que el sync nunca trajo lo viejo). Marcarla
            # como "sin cargar" sería inventar una tarea pendiente.
            "pre_corte": _antes_del_corte(c.get("fecha_recepcion")),
        })

    cruce = _buscar_compras(refs)
    por_bod = _buscar_por_bod({str(f.get("bod") or "") for f in filas})
    for f in filas:
        # 1º por BOD (el documento de la entrega), 2º por nº de factura.
        bod = str(f.get("bod") or "").strip().upper()
        hits = por_bod.get(bod) if bod else None
        f["cruce_por"] = "bod" if hits else None
        if not hits:
            key = (f.get("prov"), f.get("fact_num"))
            hits = cruce.get(key) if key[0] and key[1] is not None else None
            if hits:
                f["cruce_por"] = "factura"
        if not hits:
            continue
        est = _estado_pago(hits)
        f["compra"] = {
            "ids": [h["id_compra"] for h in hits],
            "first_id": hits[0]["id_compra"],
            "importe_total": est["importe"],
            "n": len(hits),
            "tipo": (hits[0].get("tipo") or "").strip(),
            "items": [
                {"fecha": h.get("fecha"),
                 "importe": float(h.get("importe") or 0),
                 "id_compra": h.get("id_compra"),
                 "kg": float(h.get("kg") or 0),
                 "usuario_crea": h.get("usuario_crea")}
                for h in sorted(hits, key=lambda x: str(x.get("fecha") or ""),
                                reverse=True)
            ],
        }
        f["fuente"] = "compra"
        f["importe_programa"] = est["importe"]
        f["pagada"] = est["pagada"]
        f["saldo"] = est["saldo"]
        f["parcial"] = est["parcial"]
    return filas


def hilado_local_recibido_mes(yy: int, mm: int) -> dict:
    """Kg y $ (factura de Asinfo, SIN IVA) de compras LOCALES de hilo RECIBIDAS en el mes.

    El análogo local de `asinfo_service.hilado_recibido_mes` (que sólo cuenta
    importaciones). "Recibida en el mes" = con fecha de recepción a la bodega 51
    cuyo prefijo YYYY-MM coincide, igual corte que las importaciones. El $ es
    la factura de Asinfo sin IVA — la MISMA plata con la que el motor crea el
    pasivo. Una entrega cuya factura todavía no está suma kg y $ 0.

    Dueña 2026-07-30: "la fábrica importa hilo pero también hace compras locales;
    en Ingreso de hilado hay que sumarlas". Se usa en el cuadro MOVIMIENTOS del
    flujo (fila Ingresos) y en la valuación del hilado (mov_hilado_valuacion),
    para que Ingresos = importación + compra local y la columna cierre.

    Fail-soft: {"kg": 0.0, "us": 0.0} si Asinfo no responde.
    """
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    try:
        filas = compras_locales_con_cruce()
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper el flujo/balance
        return {"kg": 0.0, "us": 0.0}
    kg = 0.0
    us = 0.0
    for f in filas or []:
        if not f.get("recibida"):
            continue
        if not str(f.get("fecha_recepcion") or "").startswith(pref):
            continue
        _kg = float(f.get("kg") or 0.0)
        kg += _kg
        if f.get("importe_sugerido"):
            us += float(f["importe_sugerido"]) / IVA
    return {"kg": kg, "us": us}


def estado_pago_de_compras(compras: list[dict] | None) -> dict:
    """Estado de pago de las compras ya cruzadas de una IMPORTACIÓN.

    La pantalla muestra el mismo marcador (pagada / debe / debe X) para las dos
    clases de fila, así que el cálculo tiene que ser el mismo. Las importaciones
    ya traen sus compras desde `modules.importaciones.service`; acá sólo se les
    consulta el estado por id.
    """
    ids = [c.get("id_compra") for c in (compras or []) if c.get("id_compra")]
    if not ids:
        return {"importe": 0.0, "pagada": False, "saldo": 0.0, "parcial": False}
    try:
        rows = db.fetch_all(
            """
            SELECT c.id_compra, c.importe,
                   COALESCE((
                       SELECT SUM(pd.importe)
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ), 0) AS saldo_pasivo,
                   EXISTS (
                       SELECT 1
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ) AS tiene_posdat,
                   -- Sin vínculo mov_doble (histórica del dBase): el BANC
                   -- importado NO sirve — el sync del dBase está parado desde
                   -- el 10/07 y ese campo quedó congelado ANTES del pago. Manda
                   -- scintela.posdat, que se reconcilia aparte y sí está al
                   -- día: sin posdatado abierto que la represente = PAGADA.
                   -- MISMA expresión que la columna "Pagada" de /compras: dos
                   -- pantallas diciendo cosas distintas de la misma compra
                   -- sería peor que el bug.
                   ((c.no_banco IS NOT NULL AND c.no_banco <> 0)
                    OR (c.cuenta_pagada IS NOT NULL AND btrim(c.cuenta_pagada) <> '')
                    OR NOT EXISTS (
                          SELECT 1 FROM scintela.posdat pd
                           WHERE UPPER(TRIM(COALESCE(pd.prov, ''))) = UPPER(TRIM(COALESCE(c.codigo_prov, '')))
                             AND COALESCE(pd.banc, 0) = 0
                             AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                             -- Un posdatado de $0,00 no es una deuda.
                             AND ABS(COALESCE(pd.importe, 0)) > 0.01
                             -- Con concepto de los dos lados manda el concepto:
                             -- es lo único que distingue CUOTAS del mismo
                             -- importe y fecha (caso CN, 6 × 20.200 el mismo
                             -- día). El posdat del dBase lo escribe con relleno,
                             -- por eso se colapsan los espacios. Si alguno viene
                             -- vacío (caso HY) se cae a fecha + importe.
                             AND CASE
                                   WHEN btrim(COALESCE(pd.concepto, '')) <> ''
                                    AND btrim(COALESCE(c.concepto, ''))  <> ''
                                   THEN regexp_replace(upper(btrim(pd.concepto)), '[[:space:]]+', ' ', 'g')
                                      = regexp_replace(upper(btrim(c.concepto)),  '[[:space:]]+', ' ', 'g')
                                   ELSE pd.fecha = c.fecha
                                    AND ABS(COALESCE(pd.importe, 0) - COALESCE(c.importe, 0)) < 0.01
                                 END
                    ))
                       AS pagada_legacy
              FROM scintela.compra c
             WHERE c.id_compra = ANY(%s)
            """,
            ([int(i) for i in ids],),
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("estado_pago_de_compras falló: %s", e)
        return {"importe": 0.0, "pagada": False, "saldo": 0.0, "parcial": False}
    return _estado_pago(rows)


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------
def _antes_del_corte(fecha_recepcion: str | None) -> bool:
    try:
        return date.fromisoformat(str(fecha_recepcion)[:10]) < CORTE
    except (TypeError, ValueError):
        return True  # sin fecha usable → no se carga


def _ajustar_a_la_factura(f: dict, base: dict, *, dry_run: bool,
                          usuario: str) -> dict | None:
    """Si la compra no dice lo que dice la factura de Asinfo, la corrige.

    Dos casos, un mismo mecanismo:
      · la entrega CRECIÓ (Tamara 2026-09-16: *"con cada entrega, y se
        ajusta"*) — una recepción se sigue pistoleando después de creada y
        los kg del BOD suben entre corridas;
      · la compra se creó con el TARIFARIO viejo (hasta el 17/09/2026) y
        la factura dice otra cosa — Tamara: *"cambiamos el monto de las que
        ya están cargadas"*. Son 13 compras de HY, $ 18.772 de deuda que
        faltaba, y se corrigen acá en la primera corrida, sin cargar nada
        extra.

    `compras.queries.editar` corrige el importe y **propaga al posdat
    hermano** en la misma transacción; si la compra ya está pagada, el
    propio `editar` la rechaza y queda anotada en el detalle.

    Sólo ajusta:
      · compras que creó ESTE motor (`usuario_crea` = MARCADOR_CARGA) — una
        tipeada a mano (la 37649 de Andrés) no se toca sola;
      · cuando hay UNA sola compra de esa entrega (si hay varias, el reparto
        no es nuestro: va a la alarma de duplicados);
      · cuando el importe nuevo difiere en más de un centavo.

    De paso estampa el BOD en `comprobante` si la compra vieja no lo tenía
    (las de antes del 16/09 cruzan por nº de factura): la corrida siguiente
    ya la encuentra por el documento.

    Devuelve la fila del detalle si ajustó (o ajustaría, en dry-run), o None.
    """
    from modules.compras import queries as _compras_q

    hits = (f.get("compra") or {}).get("items") or []
    if len(hits) != 1:
        return None
    if (hits[0].get("usuario_crea") or "").strip() != MARCADOR_CARGA:
        return None
    importe_nuevo = f.get("importe_sugerido")
    if not importe_nuevo:
        return None
    importe_viejo = float(hits[0].get("importe") or 0)
    if abs(float(importe_nuevo) - importe_viejo) <= 0.01:
        return None
    if f.get("cruce_por") != "bod":
        # Cruzó por nº de factura: sólo si la compra es de ESTA entrega (mismos
        # kilos). Una factura que llegó en dos tandas y tiene una sola compra
        # vieja por el total no se recorta a la mitad.
        kg_compra = float(hits[0].get("kg") or 0)
        if abs(kg_compra - float(f.get("kg") or 0)) > 0.5:
            return None

    kg_viejo = float(hits[0].get("kg") or 0)
    fila = {**base, "ok": True, "ajuste": True,
            "importe_previo": round(importe_viejo, 2),
            "importe": round(float(importe_nuevo), 2),
            # Para que el aviso diga QUÉ pasó: llegó más mercadería (los kg
            # subieron) o el importe se corrigió al de la factura.
            "kg_previo": round(kg_viejo, 2),
            "crecio": float(f.get("kg") or 0) > kg_viejo + 0.005}
    if dry_run:
        return fila
    try:
        _compras_q.editar(
            int(hits[0]["id_compra"]),
            importe=float(importe_nuevo),
            kg=round(float(f.get("kg") or 0), 2),
            comprobante=(str(f.get("bod") or "") or None
                         if f.get("cruce_por") != "bod" else None),
            usuario=usuario,
            observacion=(f"hilo local: la factura {f.get('fact_num')} de Asinfo "
                         f"dice ${float(importe_nuevo):.2f} (entrega {f.get('bod')}); "
                         f"la compra decía ${importe_viejo:.2f}"),
        )
    except Exception as e:  # noqa: BLE001 -- una no corta el lote
        _LOG.warning("compras_locales: no pude ajustar %s: %s", f.get("bod"), e)
        return {**base, "ok": False,
                "motivo": f"no se pudo ajustar: {str(e)[:90]}"}
    _LOG.info("hilo local: ajustada %s %.2f → %.2f", f.get("bod"),
              importe_viejo, float(importe_nuevo))
    return fila


def cargar_pendientes(*, usuario: str = "web", clave: str | None = None,
                      dry_run: bool = False, limite: int = 200) -> dict:
    """Crea las compras tipo H de las locales recibidas que faltan.

    Con `dry_run=True` NO escribe nada y devuelve el MISMO plan que ejecutaría,
    así el preview de la pantalla no puede mentir (mismo criterio que la carga
    de tejeduría).

    Devuelve {creadas, importe, salteadas, detalle:[...]} y NO levanta si una
    factura falla: la anota en el detalle con el motivo y sigue.
    """
    from modules.compras import queries as _compras_q

    filas = compras_locales_con_cruce(limite=limite)
    out: dict = {"dry_run": dry_run, "creadas": 0, "ajustadas": 0,
                 "importe": 0.0, "salteadas": 0, "detalle": []}
    if not filas:  # guarda 1 — Asinfo mudo
        return out

    creadas, importe_total, ajustadas = 0, 0.0, 0
    detalle: list[dict] = []
    # Más viejas primero: si el tope corta, se cargan las que llevan más tiempo.
    filas = sorted(filas, key=lambda f: str(f.get("fecha_recepcion") or ""))
    for f in filas:
        base = {
            "fp_numero": f.get("im_numero"),
            "bod": f.get("bod"),
            "cod": f.get("prov"),
            "proveedor": f.get("proveedor"),
            "fact_num": f.get("fact_num"),
            "producto": f.get("producto"),
            "fecha_recepcion": f.get("fecha_recepcion"),
            "kg": f.get("kg"),
            "importe": f.get("importe_sugerido"),
        }
        kg = float(f.get("kg") or 0)

        if f.get("compra"):                                    # guarda 6
            aj = _ajustar_a_la_factura(f, base, dry_run=dry_run, usuario=usuario)
            detalle.append(aj or {**base, "ok": False, "motivo": "ya tiene compra"})
            if aj:
                ajustadas += 1
            continue
        if f.get("anulada"):
            # La recepción se anuló en Asinfo y nunca llegó a tener compra:
            # no hay nada que crear (y nada que dar de baja).
            detalle.append({**base, "ok": False,
                            "motivo": "la recepción está anulada en Asinfo"})
            continue
        if not f.get("recibida") or kg <= 0:                   # guarda 2
            detalle.append({**base, "ok": False, "motivo": "todavía no recibida"})
            continue
        if _antes_del_corte(f.get("fecha_recepcion")):         # guarda 3
            detalle.append({**base, "ok": False,
                            "motivo": f"recibida antes del {CORTE.isoformat()}"})
            continue
        if not f.get("prov"):                                  # guarda 4
            detalle.append({**base, "ok": False,
                            "motivo": "proveedor sin código en el programa (RUC)"})
            continue
        if f.get("fact_num") is None:
            detalle.append({**base, "ok": False, "motivo": "factura sin número"})
            continue
        if not f.get("importe_sugerido"):                      # guarda 5
            detalle.append({**base, "ok": False,
                            "motivo": "la factura todavía no está en Asinfo"})
            continue
        if creadas >= TOPE_COMPRAS or (                        # guarda 7
                importe_total + float(f["importe_sugerido"]) > TOPE_IMPORTE):
            detalle.append({**base, "ok": False, "motivo": "tope de la corrida"})
            continue

        if dry_run:
            creadas += 1
            importe_total += float(f["importe_sugerido"])
            detalle.append({**base, "ok": True})
            continue

        try:
            res = _compras_q.crear(
                fecha=date.fromisoformat(str(f["fecha_recepcion"])[:10]),
                codigo_prov=f["prov"],
                importe=f["importe_sugerido"],
                kg=round(kg, 2),
                tipo="H",
                # Mismo concepto que escribe el FoxPro: el nº de factura del
                # proveedor. Es la clave con la que esta pantalla vuelve a
                # encontrar la compra.
                concepto=str(f["fact_num"]),
                # El DOCUMENTO de la entrega, estampado (regla de tejeduría
                # con el IFT): es lo que reconoce la corrida siguiente, lo que
                # deja que dos entregas de la misma factura sean dos pasivos,
                # y el hilo para rastrear cualquiera de las dos hacia Asinfo.
                comprobante=str(f.get("bod") or "") or None,
                clave=clave,
                usuario=MARCADOR_CARGA,
            )
        except Exception as e:  # noqa: BLE001 -- una factura no corta el lote
            detalle.append({**base, "ok": False, "motivo": str(e)[:120]})
            continue

        creadas += 1
        importe_total += float(f["importe_sugerido"])
        detalle.append({**base, "ok": True, "numero_compra": res.get("numero")})

    out["creadas"] = creadas
    out["ajustadas"] = ajustadas
    out["importe"] = round(importe_total, 2)
    out["salteadas"] = sum(1 for d in detalle if not d["ok"])
    out["detalle"] = detalle
    if (creadas or ajustadas) and not dry_run:
        asinfo_service.reset_locales_cache()
        _avisar_carga(out)
    return out


def _avisar_carga(res: dict) -> int:
    """Un aviso por compra creada. Devuelve cuántos entraron al buzón.

    Sin jerga interna (regla 30/07): el aviso dice QUÉ llegó, POR CUÁNTO y que
    ya quedó cargado. Nada de códigos de sistema ni instrucciones para el otro
    programa.
    """
    from modules.avisos import avisar as _avisar

    puestos = 0
    for d in res.get("detalle") or []:
        if not d.get("ok"):
            continue
        cod = (d.get("cod") or "?").upper()
        nombre = (d.get("proveedor") or cod).strip()
        # La clave lleva el BOD, no el nº de factura: dos entregas de la misma
        # factura son dos avisos distintos, y con la clave vieja la segunda se
        # perdía por idempotente. Las de antes del BOD caen al nº de factura.
        marca = d.get("bod") or f"f{d.get('fact_num')}"
        es_ajuste = bool(d.get("ajuste"))
        clave = f"hilo-local:{cod}:{marca}" + (":aj" if es_ajuste else "")
        if es_ajuste:
            titulo = (f"{cod} {nombre} · factura {d.get('fact_num')} · "
                      f"ahora $ {num_es(float(d.get('importe') or 0), 2)}")
            # El 17/09 las 13 correcciones de HY salieron con "Llegó más
            # mercadería" sin que hubiera llegado nada: el texto tiene que
            # decir lo que pasó de verdad.
            por_que = ("Llegó más mercadería de la misma entrega"
                       if d.get("crecio")
                       else "El importe pasó a ser el de la factura de Asinfo")
            detalle = (f"{por_que} · antes "
                       f"$ {num_es(float(d.get('importe_previo') or 0), 2)} · "
                       f"{num_es(float(d.get('kg') or 0), 2)} kg")
        else:
            titulo = (f"{cod} {nombre} · factura {d.get('fact_num')} · "
                      f"$ {num_es(float(d.get('importe') or 0), 2)}")
            detalle = ("Se cargó a compras · "
                       f"{num_es(float(d.get('kg') or 0), 2)} kg")
        puestos += bool(_avisar(
            fuente="hilo-local",
            titulo=titulo,
            detalle=detalle,
            importe=round(float(d.get("importe") or 0), 2),
            cantidad=1,
            url="/importaciones",
            clave=clave[:400],
        ))
    return puestos


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo: carga lo que llegó y avisa. Nunca levanta."""
    global _auto_ultimo_ts
    res = {"corrio": False, "creadas": 0, "importe": 0.0}
    if os.environ.get("HILO_LOCAL_AUTO", "1").strip() == "0":   # guarda 8
        return res
    ahora = _time.monotonic()
    with _AUTO_LOCK:
        if _auto_ultimo_ts and (ahora - _auto_ultimo_ts) < _AUTO_INTERVALO_MIN:
            return res
        _auto_ultimo_ts = ahora
    try:
        res["corrio"] = True
        carga = cargar_pendientes(usuario=MARCADOR_CARGA)
        res["creadas"] = carga.get("creadas") or 0
        res["ajustadas"] = carga.get("ajustadas") or 0
        res["importe"] = carga.get("importe") or 0.0
        # Y lo que quedó trabado por una guarda (factura que no está, RUC
        # sin proveedor): kilos en bodega sin su deuda. Fail-soft por su cuenta —
        # que no se pueda avisar nunca frena la carga.
        try:
            res["avisadas"] = avisar_trabadas()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("compras locales (aviso trabadas): %s", e)
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("compras locales (fondo): %s", e)
    return res


# ---------------------------------------------------------------------------
# Kilos en bodega sin su deuda — la alarma
# ---------------------------------------------------------------------------
# Tamara 2026-09-16, mirando las dos recepciones trabadas desde agosto: *"igual
# tiene que haber anuncios"*.
#
# Con el disparador en la recepción el pasivo nace junto con los kilos, así que
# lo único que puede dejar una entrega sin deuda es una GUARDA: la factura no
# está en Asinfo, el RUC no mapea a ningún proveedor de PC, o Asinfo no contestó.
# Eso antes no lo miraba nadie: había que entrar a /importaciones y leer la
# columna. Medido el 16/09 había 1.882 kg de tres semanas atrás sin su pasivo.
#
# El umbral de 24 h es a propósito: por debajo de eso una entrega "sin compra"
# es sólo la corrida que todavía no pasó, y un ⚠ diario por algo legítimo
# entrena a ignorar el panel.
HORAS_PARA_ALARMA = 24


def _entregas(n: int, adjetivo: str = "") -> str:
    """«1 entrega anulada» / «3 entregas anuladas».

    Nada de «entrega(s)» ni «anulada(s)» en un texto que alguien lee: el
    adjetivo se pasa en singular y concuerda solo.
    """
    palabra = "1 entrega" if n == 1 else f"{n} entregas"
    if not adjetivo:
        return palabra
    return f"{palabra} {adjetivo}" if n == 1 else f"{palabra} {adjetivo}s"


def _dias(n: int) -> str:
    """«1 día» / «22 días»."""
    return "1 día" if n == 1 else f"{n} días"


def _horas_desde(fecha_iso: str | None) -> float | None:
    """Horas entre `fecha_iso` (YYYY-MM-DD) y hoy, hora Ecuador. None si no parsea."""
    from filters import today_ec

    try:
        d = date.fromisoformat(str(fecha_iso)[:10])
    except (TypeError, ValueError):
        return None
    return (today_ec() - d).days * 24.0


def sin_pasivo(filas: list[dict] | None = None) -> list[dict]:
    """Entregas recibidas que NO tienen su compra en el programa, con el motivo.

    Deja afuera lo que no es un problema: lo anterior al CORTE (que el motor no
    mira nunca), lo anulado en Asinfo, y lo que todavía no cumplió las horas.
    """
    if filas is None:
        try:
            filas = compras_locales_con_cruce()
        except Exception:  # noqa: BLE001 -- fail-soft
            return []
    out: list[dict] = []
    for f in filas or []:
        if f.get("compra") or not f.get("recibida") or f.get("anulada"):
            continue
        if float(f.get("kg") or 0) <= 0 or f.get("pre_corte"):
            continue
        horas = _horas_desde(f.get("fecha_recepcion"))
        if horas is not None and horas < HORAS_PARA_ALARMA:
            continue
        if not f.get("prov"):
            motivo = "el RUC de Asinfo no coincide con ningún proveedor del programa"
        elif not f.get("importe_sugerido"):
            motivo = (f"la factura de {f.get('prov')} por "
                      f"{f.get('producto') or '?'} todavía no está en Asinfo")
        else:
            motivo = "la carga automática no llegó a crearla"
        out.append({
            "bod": f.get("bod"),
            "fecha_recepcion": f.get("fecha_recepcion"),
            "dias": int((horas or 0) // 24),
            "prov": f.get("prov"),
            "proveedor": f.get("proveedor"),
            "producto": f.get("producto"),
            "fact_num": f.get("fact_num"),
            "kg": round(float(f.get("kg") or 0), 2),
            "importe_sugerido": f.get("importe_sugerido"),
            "motivo": motivo,
        })
    return sorted(out, key=lambda r: str(r.get("fecha_recepcion") or ""))


def duplicadas() -> list[dict]:
    """Entregas con MÁS DE UNA compra viva en el programa.

    Pasa si alguien la tipea a mano mientras el motor ya la cargó. Dos pasivos
    por la misma mercadería: el proveedor figura cobrando dos veces y la
    utilidad queda baja por la diferencia.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(c.comprobante)) AS bod,
                   COUNT(*) AS n,
                   ROUND(SUM(c.importe), 2) AS importe,
                   STRING_AGG(c.id_compra::text, ', '
                              ORDER BY c.id_compra) AS ids
              FROM scintela.compra c
             WHERE c.tipo = 'H'
               AND COALESCE(c.stat, '') <> 'Y'
               AND COALESCE(TRIM(c.comprobante), '') <> ''
             GROUP BY UPPER(TRIM(c.comprobante))
            HAVING COUNT(*) > 1
             ORDER BY 1
            """,
        ) or []
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("compras_locales.duplicadas falló: %s", e)
        return []
    return [{"bod": r.get("bod"), "n": int(r.get("n") or 0),
             "importe": float(r.get("importe") or 0), "ids": r.get("ids")}
            for r in rows]


def anuladas_con_compra(filas: list[dict] | None = None) -> list[dict]:
    """Entregas ANULADAS en Asinfo que siguen con su deuda viva en el programa.

    El motor no da de baja nada solo: anular una compra mueve plata y tiene su
    pantalla de confirmación. Lo que sí hace es no dejar que pase inadvertido —
    si no, queda un pasivo con un proveedor por mercadería que nunca entró, y
    la utilidad baja por ese monto hasta que alguien lo note.
    """
    if filas is None:
        try:
            filas = compras_locales_con_cruce()
        except Exception:  # noqa: BLE001 -- fail-soft
            return []
    out = []
    for f in filas or []:
        if not f.get("anulada") or not f.get("compra"):
            continue
        if f.get("pagada"):        # ya se pagó: anularla es otra conversación
            continue
        out.append({
            "bod": f.get("bod"),
            "prov": f.get("prov"),
            "proveedor": f.get("proveedor"),
            "fact_num": f.get("fact_num"),
            "kg": round(float(f.get("kg") or 0), 2),
            "importe_programa": f.get("importe_programa"),
            "ids": (f.get("compra") or {}).get("ids"),
        })
    return out


def health() -> dict:
    """{ok, alerts, stats} para /admin/health/all.

    Si Asinfo no contesta, `ok` con `sin_datos`: no poder mirar no es lo mismo
    que estar mal, y un health que se pone rojo cada vez que Metabase tose
    deja de servir.
    """
    alerts: list[dict] = []
    try:
        filas = compras_locales_con_cruce()
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "alerts": [], "stats": {"sin_datos": str(e)[:120]}}
    if not filas:
        return {"ok": True, "alerts": [], "stats": {"sin_datos": "Asinfo no contestó"}}

    faltan = sin_pasivo(filas)
    dobles = duplicadas()
    fantasmas = anuladas_con_compra(filas)
    if faltan:
        kg = sum(f["kg"] for f in faltan)
        quienes = ", ".join(
            f"{f['prov'] or f['proveedor'] or '?'} {f['bod']} "
            f"(hace {_dias(f['dias'])})" for f in faltan[:4])
        alerts.append({
            "severity": "high",
            "category": "hilo_local_sin_pasivo",
            "msg": (
                f"Hay {_entregas(len(faltan))} de hilo por {num_es(kg, 2)} "
                f"kg en bodega sin su deuda cargada ({quienes}). Se resuelven "
                f"en Ingreso de hilado."
            ),
        })
    if dobles:
        alerts.append({
            "severity": "high",
            "category": "hilo_local_duplicado",
            "msg": (
                f"Hay {_entregas(len(dobles))} de hilo con más de una compra "
                f"viva ({', '.join(d['bod'] for d in dobles[:4])}). El "
                f"proveedor figura cobrando dos veces."
            ),
        })
    if fantasmas:
        total = sum(float(f.get("importe_programa") or 0) for f in fantasmas)
        alerts.append({
            "severity": "high",
            "category": "hilo_local_anulada_con_deuda",
            "msg": (
                f"Hay {_entregas(len(fantasmas), 'anulada')} en Asinfo con "
                f"su deuda viva en el programa por $ {num_es(total, 2)} "
                f"({', '.join(str(f.get('bod')) for f in fantasmas[:4])}). "
                f"Se dan de baja por Compras."
            ),
        })
    return {
        "ok": not alerts,
        "alerts": alerts,
        "stats": {
            "recepciones": len(filas),
            "sin_pasivo": faltan,
            "duplicadas": dobles,
            "anuladas_con_deuda": fantasmas,
            "horas_para_alarma": HORAS_PARA_ALARMA,
        },
    }


def avisar_trabadas() -> int:
    """Un aviso en la campanita por entrega trabada. Devuelve cuántos entraron.

    Clave idempotente por BOD: mientras siga trabada no repite el aviso, y
    cuando se resuelva simplemente deja de haber caso.
    """
    from modules.avisos import avisar as _avisar

    puestos = 0
    for f in sin_pasivo():
        cod = (f.get("prov") or f.get("proveedor") or "?").strip()[:40]
        puestos += bool(_avisar(
            fuente="hilo-local",
            titulo=(f"{cod} · {num_es(f['kg'], 2)} kg en bodega sin la deuda "
                    f"cargada · hace {_dias(f['dias'])}"),
            detalle=f["motivo"],
            importe=round(float(f.get("importe_sugerido") or 0), 2),
            cantidad=1,
            url="/importaciones",
            clave=f"hilo-local-trabada:{f.get('bod')}"[:400],
        ))
    return puestos
