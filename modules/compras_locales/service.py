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
  * el importe sale de un TARIFARIO, no de Asinfo (dueña: *"asinfo nunca nos
    importa en plata... tomá el tarifario con montos de dbase"*);
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
 5. **Tarifa resuelta** o se saltea — nunca inventamos un precio.
 6. **Ya cruzada** — si la factura ya tiene una compra en PC (match por
    proveedor + nº de factura), no se vuelve a cargar. Idempotente.
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

#: El dBase graba COMPRAS.IMPORTE **con IVA**; el tarifario vive sin IVA.
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


def _importe(kg: float, tarifa: float | None) -> float | None:
    """kg × tarifa → base, y sobre la BASE REDONDEADA el IVA. En dos pasos.

    Así factura el proveedor y así queda en el dBase: la base es el renglón de
    la factura (redondeado al centavo) y el IVA se calcula sobre eso. Verificado
    contra el caso real HY 37649 — 499,14 kg × 2,90 = 1.447,51 (que es exacto el
    total sin IVA de Asinfo) y × 1,15 = **1.664,64**, la compra que está cargada
    en /compras. En un solo paso daría 1.664,63.

    Ojo: el resultado es una APROXIMACIÓN y tiene que serlo — la tarifa del
    tarifario es un promedio (dueña 2026-07-30: *"el tarifario hace un
    promedio"*) y la factura real puede venir partida en lotes, cada uno con su
    redondeo. En facturas de ~$42.000 el desvío observado es de ~$0,40.
    """
    if not tarifa or kg <= 0:
        return None
    return round(round(kg * float(tarifa), 2) * IVA, 2)


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
                   c.importe, c.kg, c.tipo, c.concepto,
                   TO_CHAR(c.fecha, 'YYYY-MM-DD') AS fecha,
                   COALESCE((
                       SELECT SUM(pd.importe)
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ), 0) AS saldo_pasivo,
                   EXISTS (
                       SELECT 1
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ) AS tiene_posdat,
                   ((c.no_banco IS NOT NULL AND c.no_banco <> 0)
                    OR (c.cuenta_pagada IS NOT NULL AND btrim(c.cuenta_pagada) <> ''))
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
        tarifa        — $/kg sin IVA resuelto, o None
        importe_sugerido — kg × tarifa × IVA, o None
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
    tarifas = _tar.listar_tarifas()

    filas: list[dict] = []
    refs: set[tuple[str, int]] = set()
    for c in crudas:
        prov = por_ruc.get((c.get("ruc") or "").strip())
        fact = c.get("fact_num")
        if prov and fact is not None:
            refs.add((prov, int(fact)))
        kg = float(c.get("kg") or 0)
        tarifa = _tar.resolver(tarifas, prov or "", c.get("producto")) if prov else None
        filas.append({
            "origen": "compra",
            "im_numero": c.get("fp_numero"),
            "fecha": c.get("fecha"),
            "fecha_recepcion": c.get("fecha_recepcion"),
            "fecha_recepcion_pc": None,
            "recibida": bool(c.get("recibida")),
            "bod": c.get("bod"),
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
            "tarifa": tarifa,
            "importe_sugerido": _importe(kg, tarifa),
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
        })

    cruce = _buscar_compras(refs)
    for f in filas:
        key = (f.get("prov"), f.get("fact_num"))
        hits = cruce.get(key) if key[0] and key[1] is not None else None
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
                 "id_compra": h.get("id_compra")}
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
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ), 0) AS saldo_pasivo,
                   EXISTS (
                       SELECT 1
                         FROM scintela.mov_doble md
                         JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                   ) AS tiene_posdat,
                   ((c.no_banco IS NOT NULL AND c.no_banco <> 0)
                    OR (c.cuenta_pagada IS NOT NULL AND btrim(c.cuenta_pagada) <> ''))
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
    out: dict = {"dry_run": dry_run, "creadas": 0, "importe": 0.0,
                 "salteadas": 0, "detalle": []}
    if not filas:  # guarda 1 — Asinfo mudo
        return out

    creadas, importe_total = 0, 0.0
    detalle: list[dict] = []
    # Más viejas primero: si el tope corta, se cargan las que llevan más tiempo.
    filas = sorted(filas, key=lambda f: str(f.get("fecha_recepcion") or ""))
    for f in filas:
        base = {
            "fp_numero": f.get("im_numero"),
            "cod": f.get("prov"),
            "proveedor": f.get("proveedor"),
            "fact_num": f.get("fact_num"),
            "producto": f.get("producto"),
            "fecha_recepcion": f.get("fecha_recepcion"),
            "kg": f.get("kg"),
            "tarifa": f.get("tarifa"),
            "importe": f.get("importe_sugerido"),
        }
        kg = float(f.get("kg") or 0)

        if f.get("compra"):                                    # guarda 6
            detalle.append({**base, "ok": False, "motivo": "ya tiene compra"})
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
        if not f.get("tarifa") or not f.get("importe_sugerido"):   # guarda 5
            detalle.append({**base, "ok": False,
                            "motivo": f"falta tarifa para {f.get('producto') or '?'}"})
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
    out["importe"] = round(importe_total, 2)
    out["salteadas"] = sum(1 for d in detalle if not d["ok"])
    out["detalle"] = detalle
    if creadas and not dry_run:
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
        clave = f"hilo-local:{cod}:{d.get('fact_num')}"
        puestos += bool(_avisar(
            fuente="hilo-local",
            titulo=f"{cod} {nombre} · factura {d.get('fact_num')} · "
                   f"$ {num_es(float(d.get('importe') or 0), 2)}",
            detalle=f"Se cargó a compras · {num_es(float(d.get('kg') or 0), 2)} kg",
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
        res["importe"] = carga.get("importe") or 0.0
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("compras locales (fondo): %s", e)
    return res
