"""Vigía de facturas ANULADAS en Asinfo — que se vayan solas de PC.

TMT 2026-07-28 (dueña, caso 180441): *"esta factura fue anulada, no debería
estar sumando en la venta de hoy... tenés que diagnosticar así se va sola"*.

QUÉ PASÓ
--------
`001-099-000180441` (DSS, 86,60 kg, $871,96) se creó en Asinfo el 27/07 a las
15:13 y se ANULÓ a las 15:51 (`fc.estado = 0`, `descripcion_anulacion =
"ERROR DE PRODUCTO"`; re-emitida como 180449). La auto-carga del día
(`_auto_cargar_facturas_hoy`, cada 120 s) la levantó DENTRO de esa ventana de
38 minutos, cuando todavía estaba viva, y la escribió en PC como
`usuario_crea='asinfo-carga'` → cuenta en cartera y en la venta del día.

El filtro de origen está BIEN: la card 199 pide `fc.estado IN (1, 4, 16)` y
`asinfo.service.facturas_periodo()` tiene además la red de seguridad que
descarta `estado = 0`. Pero ese filtro sólo protege ANTES de cargar. Lo que
faltaba es el paso simétrico: **volver a mirar lo ya cargado**. Una anulación
posterior en Asinfo era, hasta hoy, invisible para PC.

QUÉ HACE ESTE MÓDULO
--------------------
Re-consulta Asinfo por la ventana de los últimos N días y anula en PC
(`stat='X'`, vía el MISMO `queries.anular` de la pantalla) las facturas que
PC trajo de Asinfo y que Asinfo ya no reporta vivas. `stat='X'` sale de
TOTF/cartera/ventas (todos los queries filtran `stat IN ('Z','A','',' ')`),
así que la factura deja de sumar sola.

FAIL-CLOSED (a propósito — anular de más es peor que anular de menos)
--------------------------------------------------------------------
1. Si Asinfo no contesta o devuelve vacío → NO se toca NADA.
2. Sólo se anulan filas con `usuario_crea='asinfo-carga'` (las que PC trajo
   sola de Asinfo). Las tipeadas a mano o venidas del dBase se REPORTAN como
   sugerencia, no se tocan.
3. Sólo se anula una factura de un día para el que Asinfo devolvió al menos
   un documento vivo. Si Asinfo no trajo nada de ese día, el día entero se
   saltea (puede ser un hueco de la card, no una anulación).
4. `queries.anular` ya bloquea las que tienen cheques aplicados vivos o
   retenciones emitidas; esas quedan listadas como `bloqueadas` para que las
   mire una persona.
5. Sólo números comparables: N° SRI (`001-099-000180441`) y las notas de
   entrega / notas de crédito de mercadería (`NTEN-10879`, `NCNT-…`), que
   salen de la MISMA card 199 con su número propio. Lo que no tiene número
   completo (las filas viejas del dBase) no se toca.

TMT 2026-08-20 (dueña, caso NTEN-10879): *"esta fue anulada"*. La nota de
entrega de BED (265,50 kg) se cargó sola el 19/08 a las 16:17 y después
desapareció de Asinfo. El vigía la miraba y la salteaba, porque el guard 5
pedía formato SRI y una NTEN no lo tiene. Quedó viva en PC sumando en la
venta del día — el mismo agujero del caso 180441, en la puerta de al lado.

GUARD 6 — AUSENCIA NO ES ANULACIÓN (TMT 2026-08-24)
---------------------------------------------------
Los cinco guards de arriba cubren todos la misma familia: **Asinfo mudo** (no
contestó, contestó vacío, ese día no trajo nada). Ninguno cubría **Asinfo
contestó INCOMPLETO**. El 21/08 a las 20:46 la card devolvió el período casi
entero pero sin dos documentos, y el vigía anuló `001-099-000182254` (KJG,
$9.421,44) y `001-099-000182327` (VGA, $7.531,62) — las dos VIVAS en el ERP,
`fc.estado = 4`, sin motivo ni fecha de anulación, idénticas a sus vecinas.
Con 503 documentos en la ventana, dos que faltan no se distinguen de dos que
anularon: por conteo el caso es indecidible.

La dueña, sobre qué hacer: *"que frene y avise"*.

Así que ya no se concluye por ausencia. Cuando un documento no viene en la
card, se le PREGUNTA A ASINFO POR SU NÚMERO (`asinfo.service.estado_de_documentos`,
que lee `factura_cliente.estado` — sirve igual para `001-099-000182254` que
para `NTEN-10879`):

  · `estado = 0`  → la anularon de verdad → se anula en PC, como siempre.
  · vivo, otro estado, no aparece, o no se pudo preguntar → **no se toca**,
    va a `frenadas` y suena la campanita para que lo mire una persona.

Anular de menos se arregla con dos clicks; anular de más deja una factura
real fuera de la cartera y de la venta, y nadie se entera.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time as _time
from datetime import date, datetime, timedelta

import db
from filters import today_ec

from . import queries

_LOG = logging.getLogger(__name__)

# Origen de las filas que el vigía SÍ puede anular solo. Es el `usuario_crea`
# que estampa la auto-carga y el botón "Cargar" de /facturas/desde-asinfo.
ORIGEN_AUTOMATICO = "asinfo-carga"

USUARIO_VIGIA = "asinfo-vigia"
MOTIVO = "Anulada en Asinfo (fc.estado=0) — detectada por el vigía"

#: Guard 6: para anular hace falta que Asinfo lo CONFIRME. Cuando no lo
#: confirma, este es el texto que explica por qué la fila quedó frenada.
FRENO_VIVA = "Asinfo la sigue dando por VIVA (estado {estado}) — no se tocó"
FRENO_SIN_RESPUESTA = "Asinfo no la reconoce por su número — no se tocó"
FRENO_SIN_CONSULTA = "no se pudo confirmar contra Asinfo — no se tocó"

_NUM_SRI_RE = re.compile(r"^\d{3}-\d{3}-\d{6,9}$")
#: Notas de entrega y notas de crédito de mercadería. Vienen de la MISMA
#: card 199 que las facturas, con su número propio ('NTEN-10879'), así que
#: se pueden comparar igual de bien. TMT 2026-08-20.
_NUM_OTRO_RE = re.compile(r"^(NTEN|NCNT)-\d+$", re.I)

# Throttle propio: la auto-carga corre cada 120 s, pero re-preguntar 7 días a
# Metabase tan seguido no aporta (el cache de facturas_periodo es 5 min).
_LOCK = threading.Lock()
_ultimo_ts = 0.0


def dias_ventana() -> int:
    try:
        v = int(os.environ.get("VIGIA_ANULADAS_DIAS", "7"))
    except (TypeError, ValueError):
        return 7
    return max(1, min(v, 60))


def _intervalo_secs() -> int:
    try:
        return max(60, int(os.environ.get("VIGIA_ANULADAS_SECS", "900")))
    except (TypeError, ValueError):
        return 900


def _numf_de_numero(numero: str):
    """'001-099-000180441' → 180441. None si no se puede."""
    if not numero:
        return None
    ultimo = str(numero).strip().split("-")[-1]
    try:
        return int(ultimo)
    except (TypeError, ValueError):
        return None


def _fecha_de(x):
    """Normaliza date/datetime/'YYYY-MM-DD...' a `date`. None si no se puede."""
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def detectar(dias: int | None = None) -> dict:
    """Compara PC vs Asinfo en la ventana y devuelve el diagnóstico.

    NO escribe nada. Devuelve::

        {ok, motivo, desde, hasta, vivas_asinfo, revisadas,
         para_anular: [fila...],      # usuario_crea='asinfo-carga'
         sugeridas:   [fila...]}      # otro origen → decide una persona

    `ok=False` significa "no pude comparar" (Asinfo mudo) → el llamador NO
    debe tocar nada.
    """
    dias = dias_ventana() if dias is None else max(1, int(dias))
    hasta = today_ec()
    desde = hasta - timedelta(days=dias - 1)
    out: dict = {
        "ok": False, "motivo": "", "desde": desde, "hasta": hasta,
        "vivas_asinfo": 0, "revisadas": 0, "para_anular": [], "sugeridas": [],
        # Guard 6: candidatas que Asinfo NO confirmó como anuladas. No se
        # tocan; van a la campanita para que las mire una persona.
        "frenadas": [],
    }

    try:
        from modules.asinfo import service as asinfo_service
        asinfo_rows = asinfo_service.facturas_periodo(desde, hasta) or []
    except Exception as e:  # noqa: BLE001 — fail-closed, nunca rompe la pantalla
        out["motivo"] = f"Asinfo no respondió ({e})"
        return out
    if not asinfo_rows:
        # Guard 1: sin datos no se puede afirmar que algo esté anulado.
        out["motivo"] = "Asinfo no devolvió documentos en la ventana"
        return out

    vivas_completo: set[str] = set()
    vivas_numf: set[int] = set()
    dias_con_datos: set = set()
    for r in asinfo_rows:
        numero = (r.get("numero") or "").strip()
        if numero:
            vivas_completo.add(numero)
            n = _numf_de_numero(numero)
            if n is not None:
                vivas_numf.add(n)
        f = _fecha_de(r.get("fecha"))
        if f:
            dias_con_datos.add(f)
    out["vivas_asinfo"] = len(vivas_completo)
    out["ok"] = True

    pc_rows = db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, codigo_cli,
               importe, abono, saldo, stat, usuario_crea
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat, '') <> 'X'
         ORDER BY fecha, numf
        """,
        (desde, hasta),
    ) or []
    out["revisadas"] = len(pc_rows)

    for f in pc_rows:
        numero = (f.get("numf_completo") or "").strip()
        es_sri = bool(_NUM_SRI_RE.match(numero))
        if not (es_sri or _NUM_OTRO_RE.match(numero)):
            continue  # Guard 5: sin número comparable no se concluye nada
        fecha = _fecha_de(f.get("fecha"))
        if fecha not in dias_con_datos:
            continue  # Guard 3: ese día Asinfo no trajo nada → no concluir
        if numero in vivas_completo:
            continue
        numf = f.get("numf")
        try:
            # El fallback por N° suelto es sólo para las SRI: el `numf` de una
            # NTEN es corto y se repite (el 10879 es también una factura vieja
            # de otro cliente), así que compararlas por ahí las daría vivas
            # para siempre. Con N° completo alcanza: la card las trae enteras.
            if es_sri and numf is not None and int(numf) in vivas_numf:
                continue
        except (TypeError, ValueError) as _e:
            from modules._lib.silencios import avisar
            avisar(__name__, "detectar", _e, nivel="debug")
        fila = {
            "id_factura": f.get("id_factura"),
            "numf": numf,
            "numf_completo": numero,
            "fecha": fecha,
            "codigo_cli": f.get("codigo_cli"),
            "importe": f.get("importe"),
            "abono": f.get("abono"),
            "saldo": f.get("saldo"),
            "stat": f.get("stat"),
            "usuario_crea": f.get("usuario_crea"),
        }
        if (f.get("usuario_crea") or "") == ORIGEN_AUTOMATICO:
            out["para_anular"].append(fila)   # Guard 2
        else:
            out["sugeridas"].append(fila)

    _confirmar_contra_asinfo(out)
    return out


def _confirmar_contra_asinfo(out: dict) -> None:
    """Guard 6 — mueve de `para_anular` a `frenadas` todo lo que Asinfo no
    confirme como anulado (`fc.estado = 0`).

    Trabaja sobre el dict de `detectar()`, in place. Las `sugeridas` no se
    tocan: esas ya las decide una persona, así que no hace falta frenarlas —
    pero sí se les anota lo que Asinfo contestó, que es justo lo que esa
    persona necesita para decidir.
    """
    candidatas = list(out.get("para_anular") or []) + list(out.get("sugeridas") or [])
    if not candidatas:
        return
    numeros = [c.get("numf_completo") for c in candidatas if c.get("numf_completo")]
    try:
        from modules.asinfo import service as asinfo_service
        estados = asinfo_service.estado_de_documentos(numeros)
    except Exception as e:  # noqa: BLE001 — nunca rompe la pantalla
        _LOG.warning("vigia-anuladas: no pude confirmar contra Asinfo: %s", e)
        estados = None

    def _veredicto(fila: dict) -> str:
        """'' = confirmada (se puede anular). Si no, el motivo del freno."""
        if estados is None:
            return FRENO_SIN_CONSULTA
        dato = estados.get((fila.get("numf_completo") or "").strip().upper())
        if dato is None:
            return FRENO_SIN_RESPUESTA
        if int(dato.get("estado", -1)) != asinfo_service.ESTADO_ANULADO:
            return FRENO_VIVA.format(estado=dato.get("estado"))
        fila["motivo_asinfo"] = dato.get("motivo") or ""
        return ""

    confirmadas, frenadas = [], []
    for fila in out.get("para_anular") or []:
        freno = _veredicto(fila)
        if freno:
            frenadas.append({**fila, "freno": freno})
        else:
            confirmadas.append(fila)
    out["para_anular"] = confirmadas
    out["frenadas"] = frenadas
    for fila in out.get("sugeridas") or []:
        fila["freno"] = _veredicto(fila)

    if frenadas:
        _avisar_frenadas(frenadas)


def _avisar_frenadas(frenadas: list[dict]) -> None:
    """La campanita. Fail-soft: un aviso que no entra no frena al vigía."""
    try:
        from modules.avisos.queries import avisar
    except Exception:  # noqa: BLE001
        return
    for fila in frenadas:
        numero = (fila.get("numf_completo") or "").strip()
        try:
            avisar(
                fuente="facturas",
                nivel="alerta",
                titulo=(
                    f"Revisar {numero} — Asinfo dejó de reportarla pero no "
                    "dice que esté anulada"
                ),
                detalle=(
                    f"{fila.get('codigo_cli') or ''} · "
                    f"$ {float(fila.get('importe') or 0):,.2f} · "
                    f"{fila.get('freno') or ''}"
                )[:150],
                url="/facturas/anuladas-asinfo",
                # Una vez por documento: el vigía repasa la misma ventana
                # cada 15 minutos y sin clave el buzón se llenaría.
                clave=f"vigia-frenada-{numero}",
            )
        except Exception as e:  # noqa: BLE001
            _LOG.warning("vigia-anuladas: no pude avisar de %s: %s", numero, e)


def correr(dias: int | None = None, *, dry_run: bool = False) -> dict:
    """Detecta y (si no es dry-run) anula. Idempotente y fail-soft.

    Devuelve el dict de `detectar()` más ``anuladas`` y ``bloqueadas``.
    """
    res = detectar(dias)
    res["anuladas"] = []
    res["bloqueadas"] = []
    res.setdefault("frenadas", [])
    if not res["ok"] or dry_run:
        return res
    for fila in res["para_anular"]:
        try:
            queries.anular(
                int(fila["id_factura"]), motivo=MOTIVO, usuario=USUARIO_VIGIA,
            )
            res["anuladas"].append(fila)
            _LOG.info(
                "vigia-anuladas: %s (%s $%s) anulada — Asinfo ya no la reporta",
                fila["numf_completo"], fila["codigo_cli"], fila["importe"],
            )
        except ValueError as e:
            # Guard 4: cheques aplicados / retenciones emitidas → a mano.
            res["bloqueadas"].append({**fila, "motivo": str(e)})
        except Exception as e:  # noqa: BLE001 — una fila mala no frena al resto
            res["bloqueadas"].append({**fila, "motivo": f"error: {e}"})
    return res


def correr_si_toca() -> dict:
    """Envoltorio con throttle+lock para engancharlo a la auto-carga.

    Devuelve ``{"corrio": bool, "anuladas": int, "bloqueadas": int}``.
    Nunca levanta: la pantalla de facturas jamás debe romperse por esto.
    """
    global _ultimo_ts
    salida = {"corrio": False, "anuladas": 0, "bloqueadas": 0, "frenadas": 0}
    if os.environ.get("VIGIA_ANULADAS", "1").strip() == "0":
        return salida
    ahora = _time.monotonic()
    if ahora - _ultimo_ts < _intervalo_secs():
        return salida
    if not _LOCK.acquire(blocking=False):
        return salida
    try:
        _ultimo_ts = _time.monotonic()
        res = correr()
        salida["corrio"] = True
        salida["anuladas"] = len(res.get("anuladas") or [])
        salida["bloqueadas"] = len(res.get("bloqueadas") or [])
        salida["frenadas"] = len(res.get("frenadas") or [])
    except Exception as e:  # noqa: BLE001
        _LOG.warning("vigia-anuladas: ciclo falló: %s", e)
    finally:
        try:
            _LOCK.release()
        except RuntimeError as _e:
            from modules._lib.silencios import avisar
            avisar(__name__, "correr_si_toca", _e, nivel="debug")
    return salida
