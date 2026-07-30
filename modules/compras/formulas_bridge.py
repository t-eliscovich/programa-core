"""Puente de compras de colorantes/químicos: formulas_app → Programa Core.

formulas_app registra las compras de químicos al recibirlas (tabla
`public.compras` en la DB `postgres` del mismo RDS: producto, fecha ISO,
proveedor, factura, cantidad, precio_us SIN IVA). Este módulo las trae a
`scintela.compra` vía `queries.crear()` (tipo Q) — que es lo que genera el
pasivo (`scintela.posdat` banc=0) — con:

    - mapping de proveedores formulas → PC (SEY→SY, AVQ→AQ, PRO→PO, NQ→NQ,
      EMP→ES, QSI→QI). COLO (COLOURTEX) se EXCLUYE: es importación y entra
      por otro circuito (banc=9, con gastos de importación que formulas no
      conoce).
    - Nº de factura normalizado a la convención del programa: sin ceros a la
      izquierda ('0085' → '85'); el campo `factura` de formulas tiene 4
      caracteres, así que los proveedores con numeración más larga pierden el
      prefijo y hay que reponerlo (SY '2444' → '22444', QI '4904' → '104904').
      Concepto en formato dBase: factura + día.
    - IVA por proveedor: 15% default, 0% para ES (sal). El importe cargado
      es el total c/IVA, como carga el dBase.
    - dedup: una compra de formulas NO se carga si ya existe en
      scintela.compra una compra del mismo proveedor+mes con el mismo número
      de factura (token del concepto) o con importe equivalente (la carga
      manual del dBase suele llegar por el sync).

Todo fail-soft: si el pool `formulas_db` no está configurado o la query
rompe, `estado_mes()` devuelve disponible=False y `sincronizar_mes()` no
crea nada. El host nunca se rompe por esto.

El sync automático corre desde el hilo de fondo (`correr_si_toca`, cada 30
min), a diario desde scripts/procesa_provisiones_mensual.py (el cron del
Scheduled Task) y a demanda desde /compras/desde-formulas.
Para apagarlo rápido: env FORMULAS_COMPRAS_AUTOSYNC=0.

Las facturas del DÍA EN CURSO también se cargan (dueña 2026-07-30: "dejá de
poner topes que entorpecen"). Antes se dejaban para el día siguiente porque en
formulas la factura CRECE mientras Andrés la tipea (visto el 17/07: la 0133
pasó de 3.779 a 9.930 en la misma tarde). En vez de esperar, el puente
AUTO-CORRIGE: cada corrida compara el importe de formulas contra el de la
compra que él mismo creó y, si cambió, la edita (queries.editar propaga el
nuevo importe al posdat hermano). La corrección es exacta y acotada: sólo
toca compras con usuario_crea 'formulas-%', NO pagadas y que NADIE editó a
mano (usuario_modifica vacío o del puente) — así no pisa el arreglo manual de
una factura con IVA mixto.

Coexistencia con el sync del dBase (mientras el dBase viva):
    - scripts/import_dbf.py preserva las compras usuario_crea='formulas-auto'
      a través del TRUNCATE, salvo que el DBF traiga una gemela (mismo
      proveedor + importe + mes) → dBase gana, la copia del puente se absorbe.
    - el posdat que creó el puente lo matchea el posdat-reconcile por
      (prov, importe), igual que cualquier posdat de compra.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from dataclasses import asdict, dataclass
from datetime import date

import db
from modules._lib import formulas_db

log = logging.getLogger("programa_core.compras.formulas_bridge")

# ── Configuración del puente ────────────────────────────────────────────────

# formulas_app → codigo_prov de scintela.proveedor. Verificado por montos
# (julio 2026). Si formulas agrega un proveedor nuevo, aparece como
# 'sin_mapear' en la pantalla hasta que se agregue acá.
PROV_MAP = {
    "SEY": "SY",   # SEYQUIN CIA.LTDA.
    "AVQ": "AQ",   # MAYRA ROSERO (AV Química)
    "PRO": "PO",   # PROVITEX
    "NQ": "NQ",    # ANDESCHEMIE
    "EMP": "ES",   # CECILIA FREIRE (sal en grano)
    "QSI": "QI",   # Q.S.I. (colorantes poliéster FORON)
}

# El campo `factura` de formulas tiene 4 caracteres: un proveedor cuya
# numeración real es más larga llega TRUNCADO por la izquierda. Acá va el
# prefijo que hay que reponer, por proveedor PC. Verificado contra el dBase:
#   SY  22444 → formulas '2444'   (SEYQUIN, 5 dígitos)
#   QI 104250 → formulas '4250'   (Q.S.I., 6 dígitos; la 104904 del 28/07 es
#              la que Andrés marcó como faltante y llegó como '4904')
PREFIJO_TRUNCADO = {"SY": "2", "QI": "10"}

# Proveedores de formulas que NO pasan por este puente.
PROV_EXCLUIDOS = {
    "COLO",  # COLOURTEX — importación: banc=9, gastos de importación aparte.
}

IVA_DEFAULT = 0.15
# IVA por proveedor PC. La sal es 0%. OJO: algunas facturas SY mezclan
# ítems 15% y 0% — el importe queda aproximado por arriba; se corrige
# editando la compra (pantalla /compras → Editar), el posdat se ajusta solo.
IVA_POR_PROV = {"ES": 0.0}

# Tolerancia de matching por importe (para reconocer cargas manuales del
# dBase con IVA mixto: el total real cae entre s/IVA y s/IVA*1.15).
_TOL_ABS = 0.5


_AUTO_LOCK = threading.Lock()
_auto_ultimo_ts = 0.0
_AUTO_INTERVALO_MIN = 1800.0  # 30 min entre corridas de fondo


def autosync_habilitado() -> bool:
    """El automático se apaga con FORMULAS_COMPRAS_AUTOSYNC=0 (default: ON)."""
    return os.environ.get("FORMULAS_COMPRAS_AUTOSYNC", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


@dataclass(frozen=True)
class FilaPuente:
    """Una factura de formulas (agrupada) con su estado contra PC."""

    proveedor_formulas: str
    proveedor_pc: str | None       # None si sin_mapear / excluida
    factura_formulas: str
    factura_pc: str | None         # normalizada a convención del programa
    fecha: date | None
    kg: float
    importe_siva: float
    iva_pct: float
    importe_con_iva: float
    estado: str                    # cargada | pendiente | excluida | sin_mapear
    detalle_match: str | None = None
    id_compra_pc: int | None = None   # la compra de PC que la matcheó
    importe_pc: float | None = None   # importe con el que quedó cargada
    ajustable: bool = False           # formulas cambió → hay que corregirla

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha"] = self.fecha.isoformat() if self.fecha else None
        return d


# ── Normalización ───────────────────────────────────────────────────────────

def normalizar_factura(prov_pc: str | None, factura: str | None) -> str:
    """Número de factura como lo pone el programa.

    - Sin ceros a la izquierda: '0085' → '85' (convención AQ/PO del dBase).
    - Proveedores con numeración más larga que el campo de 4 caracteres de
      formulas: se repone el prefijo de `PREFIJO_TRUNCADO` ('2444' → '22444'
      para SY, '4904' → '104904' para QI). Sólo cuando quedan exactamente 4
      dígitos: con más ya viene completo, con menos no se puede saber (queda
      como está y el match cae al importe).
    """
    f = (factura or "").strip().upper()
    if f.isdigit():
        f = f.lstrip("0") or "0"
        pref = PREFIJO_TRUNCADO.get(prov_pc or "")
        if pref and len(f) == 4:
            f = pref + f
    return f


def concepto_pc(factura_pc: str, fecha: date | None) -> str:
    """Concepto en el formato del dBase: factura + día right-aligned (15c)."""
    if not fecha:
        return factura_pc[:15]
    return factura_pc[:13].ljust(13) + str(fecha.day).rjust(2)


def _primer_token(concepto: str | None) -> str:
    return (concepto or "").strip().split(" ")[0].strip().upper()


def _parse_fecha_iso(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:  # noqa: BLE001
        return None


# ── Lectura ─────────────────────────────────────────────────────────────────

def _rango_mes(anio: int, mes: int) -> tuple[str, str]:
    ini = f"{anio:04d}-{mes:02d}-01"
    fin = f"{anio:04d}-{mes:02d}-31"  # lex-compare sobre ISO: inclusivo
    return ini, fin


def grupos_formulas(anio: int, mes: int) -> list[dict]:
    """Facturas del mes en formulas, agrupadas por proveedor+factura."""
    ini, fin = _rango_mes(anio, mes)
    return formulas_db.fetch_all(
        """
        SELECT proveedor,
               COALESCE(factura, '')            AS factura,
               MIN(fecha)                       AS fecha,
               COALESCE(SUM(cantidad), 0)       AS kg,
               COALESCE(SUM(cantidad * precio_us), 0) AS importe_siva
          FROM compras
         WHERE fecha >= %s AND fecha <= %s
         GROUP BY proveedor, COALESCE(factura, '')
         ORDER BY MIN(fecha), proveedor
        """,
        (ini, fin),
    )


def _compras_pc_mes(anio: int, mes: int) -> list[dict]:
    """Compras ya cargadas en PC ese mes (excluye anuladas stat='Y')."""
    ini = date(anio, mes, 1)
    fin = date(anio + (1 if mes == 12 else 0), 1 if mes == 12 else mes + 1, 1)
    return db.fetch_all(
        """
        SELECT id_compra, codigo_prov, importe, concepto,
               usuario_crea, usuario_modifica, id_transaccion
          FROM scintela.compra
         WHERE fecha >= %s AND fecha < %s
           AND COALESCE(stat, '') <> 'Y'
        """,
        (ini, fin),
    )


# ── Matching ────────────────────────────────────────────────────────────────

def _es_del_puente_intacta(c: dict) -> bool:
    """¿Esta compra la creó el puente y nadie la tocó a mano?

    Condición para que el puente pueda CORREGIRLE el importe solo. Es
    deliberadamente estricta (dueña 2026-07-30: los topes valen si son
    exactos): si alguien la editó a mano — típicamente para arreglar una
    factura con IVA mixto — el puente no la vuelve a pisar. Y si ya se pagó,
    `queries.editar` la bloquea igual.
    """
    creada = (c.get("usuario_crea") or "").strip().lower()
    modif = (c.get("usuario_modifica") or "").strip().lower()
    return (
        creada.startswith("formulas")
        and (not modif or modif.startswith("formulas"))
        and c.get("id_transaccion") is None
    )


def _buscar_match(compras_pc: list[dict], prov_pc: str, factura_pc: str,
                  siva: float, civa: float) -> tuple[str, dict] | None:
    """¿Ya existe esta factura en PC? Devuelve (detalle, compra) o None.

    1. Mismo proveedor + mismo número (primer token del concepto).
    2. Mismo proveedor + número sufijo/prefijo + importe en [s/IVA, c/IVA]
       (cargas manuales con IVA mixto o número escrito distinto).

    IMPORTANTE: NO se matchea por importe solo. Las compras recurrentes
    repiten el monto exacto con facturas distintas (SOFTER FRESH 3.680,
    sal 5.000) y un match por monto marcaría "cargada" una factura que NO
    está → pasivo faltante. Verificado en vivo 2026-07-17 (SY 22521 vs
    22385, ES 7197 vs 7052, SY 22458 vs 21981).
    """
    candidatos = [c for c in compras_pc
                  if (c.get("codigo_prov") or "").strip().upper() == prov_pc]
    for c in candidatos:
        tok = _primer_token(c.get("concepto"))
        if tok and tok == factura_pc:
            return f"factura {tok} (id {c.get('id_compra')})", c
    lo = min(siva, civa) - _TOL_ABS
    hi = max(siva, civa) + _TOL_ABS
    for c in candidatos:
        tok = _primer_token(c.get("concepto"))
        imp = float(c.get("importe") or 0)
        if (tok and factura_pc
                and (tok.endswith(factura_pc) or factura_pc.endswith(tok))
                and lo <= imp <= hi):
            return f"factura ~{tok} + importe {imp:.2f} (id {c.get('id_compra')})", c
    return None


# ── Estado + sync ───────────────────────────────────────────────────────────

def estado_mes(anio: int, mes: int) -> dict:
    """Estado del puente para un mes: cada factura de formulas y si está en PC."""
    if not formulas_db.disponible():
        return {"disponible": False, "filas": [], "pendientes": 0,
                "total_pendiente": 0.0}
    grupos = grupos_formulas(anio, mes)
    compras_pc = _compras_pc_mes(anio, mes)
    filas: list[FilaPuente] = []
    for g in grupos:
        prov_f = (g.get("proveedor") or "").strip().upper()
        factura_f = (g.get("factura") or "").strip()
        fecha = _parse_fecha_iso(g.get("fecha"))
        kg = float(g.get("kg") or 0)
        siva = round(float(g.get("importe_siva") or 0), 2)
        if prov_f in PROV_EXCLUIDOS:
            filas.append(FilaPuente(prov_f, None, factura_f, None, fecha, kg,
                                    siva, 0.0, siva, "excluida",
                                    "importación — entra por su propio circuito"))
            continue
        prov_pc = PROV_MAP.get(prov_f)
        if not prov_pc:
            filas.append(FilaPuente(prov_f, None, factura_f, None, fecha, kg,
                                    siva, 0.0, siva, "sin_mapear",
                                    "proveedor de formulas sin mapping a PC"))
            continue
        iva = IVA_POR_PROV.get(prov_pc, IVA_DEFAULT)
        civa = round(siva * (1 + iva), 2)
        factura_pc = normalizar_factura(prov_pc, factura_f)
        hit = _buscar_match(compras_pc, prov_pc, factura_pc, siva, civa)
        detalle, compra_pc = hit if hit else (None, None)
        importe_pc = (float(compra_pc.get("importe") or 0)
                      if compra_pc is not None else None)
        # Auto-corrección: formulas cambió el importe DESPUÉS de que el
        # puente cargó la compra (la factura se seguía tipeando). Sólo si
        # la creó el puente y nadie la editó a mano.
        ajustable = bool(
            compra_pc is not None
            and _es_del_puente_intacta(compra_pc)
            and abs((importe_pc or 0) - civa) > 0.01
        )
        if ajustable:
            detalle = (f"{detalle} · formulas dice "
                       f"{civa:.2f} (cargada {importe_pc:.2f}) → se corrige")
        filas.append(FilaPuente(
            prov_f, prov_pc, factura_f, factura_pc, fecha, kg, siva, iva,
            civa, "cargada" if hit else "pendiente", detalle,
            id_compra_pc=(compra_pc or {}).get("id_compra"),
            importe_pc=importe_pc, ajustable=ajustable,
        ))
    pendientes = [f for f in filas if f.estado == "pendiente"]
    ajustables = [f for f in filas if f.ajustable]
    return {
        "disponible": True,
        "filas": filas,
        "pendientes": len(pendientes),
        "total_pendiente": round(sum(f.importe_con_iva for f in pendientes), 2),
        "ajustables": len(ajustables),
    }


def contar_pendientes_mes_actual(hoy: date | None = None) -> int:
    """Para el banner de /compras. Fail-soft: cualquier problema → 0."""
    try:
        from filters import today_ec
        h = hoy or today_ec()
        est = estado_mes(h.year, h.month)
        return int(est.get("pendientes") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("contar_pendientes_mes_actual falló: %s", e)
        return 0


def sincronizar_mes(anio: int, mes: int, usuario: str = "formulas-auto") -> dict:
    """Carga en PC las facturas de formulas del mes que faltan.

    Cada compra creada genera su pasivo (posdat banc=0) vía queries.crear().
    Idempotente: lo ya cargado (por este puente, por el dBase-sync o a mano)
    se reconoce por el matching y no se duplica. Cada fila se crea en su
    propia transacción — un error en una no frena las demás.

    Las facturas del día en curso TAMBIÉN se cargan; si después crecen en
    formulas, la corrida siguiente le corrige el importe a la compra (y al
    posdat hermano) vía `queries.editar`.
    """
    from modules.compras import queries as compras_queries

    est = estado_mes(anio, mes)
    if not est.get("disponible"):
        return {"disponible": False, "creadas": [], "errores": [],
                "ya_cargadas": 0, "ajustadas": []}
    creadas, errores, ajustadas = [], [], []
    for f in est["filas"]:
        if f.ajustable and f.id_compra_pc:
            try:
                res = compras_queries.editar(
                    f.id_compra_pc, importe=f.importe_con_iva, usuario=usuario,
                    observacion=(f"puente formulas: la factura {f.factura_pc} "
                                 f"cambió en formulas "
                                 f"({(f.importe_pc or 0):.2f} → "
                                 f"{f.importe_con_iva:.2f})"),
                )
                ajustadas.append({
                    "proveedor": f.proveedor_pc, "factura": f.factura_pc,
                    "importe_previo": (res or {}).get("importe_previo"),
                    "importe": f.importe_con_iva,
                })
                log.info("puente formulas: corregida %s %s %.2f → %.2f",
                         f.proveedor_pc, f.factura_pc, f.importe_pc or 0,
                         f.importe_con_iva)
            except Exception as e:  # noqa: BLE001
                log.warning("puente formulas: no pude corregir %s %s: %s",
                            f.proveedor_pc, f.factura_pc, e)
            continue
        if f.estado != "pendiente":
            continue
        try:
            res = compras_queries.crear(
                fecha=f.fecha or date(anio, mes, 1),
                codigo_prov=f.proveedor_pc,
                importe=f.importe_con_iva,
                kg=None,
                tipo="Q",
                concepto=concepto_pc(f.factura_pc, f.fecha),
                clave="F",
                pagada=False,
                usuario=usuario,
            )
            creadas.append({
                "proveedor": f.proveedor_pc,
                "factura": f.factura_pc,
                "importe": f.importe_con_iva,
                "numero": (res or {}).get("numero"),
            })
            log.info("puente formulas: cargada %s %s por %.2f",
                     f.proveedor_pc, f.factura_pc, f.importe_con_iva)
        except Exception as e:  # noqa: BLE001
            log.warning("puente formulas: %s %s falló: %s",
                        f.proveedor_pc, f.factura_pc, e)
            errores.append({
                "proveedor": f.proveedor_pc,
                "factura": f.factura_pc,
                "error": str(e),
            })
    _avisar_novedades(creadas, errores, ajustadas)
    return {
        "disponible": True,
        "creadas": creadas,
        "errores": errores,
        "ajustadas": ajustadas,
        "ya_cargadas": sum(1 for f in est["filas"] if f.estado == "cargada"),
    }


def _avisar_novedades(creadas: list, errores: list, ajustadas: list | None = None) -> None:
    """Lo cargado (y lo que falló) va a la campanita.

    TMT 2026-07-30 (dueña): *"la campanita debería funcionar también para cargas
    de tejeduría, de fórmulas"*. Va acá adentro y no en el hook del cron para
    que también avise cuando alguien aprieta el botón de /compras. Nunca levanta.
    """
    ajustadas = ajustadas or []
    if not creadas and not errores and not ajustadas:
        return
    try:
        from filters import num_es
        from modules.avisos import avisar

        if creadas:
            total = sum(float(c.get("importe") or 0) for c in creadas)
            provs = sorted({(c.get("proveedor") or "") for c in creadas})
            facturas = ",".join(sorted(str(c.get("factura") or "") for c in creadas))
            avisar(
                fuente="quimicos",
                titulo=f"Químicos · $ {num_es(total, 2)}",
                detalle=(f"Se cargaron {len(creadas)} compra"
                         f"{'' if len(creadas) == 1 else 's'} de "
                         f"{', '.join(p for p in provs if p)}"),
                importe=round(total, 2), cantidad=len(creadas),
                url="/compras", clave=f"quimicos:{facturas}"[:400],
            )
        if ajustadas:
            facturas = ",".join(sorted(str(a.get("factura") or "")
                                       for a in ajustadas))
            total = sum(float(a.get("importe") or 0) for a in ajustadas)
            avisar(
                fuente="quimicos",
                titulo=f"Químicos · se corrigió el importe de {len(ajustadas)} compra"
                       f"{'' if len(ajustadas) == 1 else 's'}",
                detalle=(f"La factura cambió en el programa de tintorería: "
                         f"{facturas} · ahora $ {num_es(total, 2)}"),
                importe=round(total, 2), cantidad=len(ajustadas),
                url="/compras",
                # el importe va en la clave: si la misma factura se corrige
                # dos veces (siguió creciendo), el 2º aviso NO se deduplica
                clave=f"quimicos:ajuste:{facturas}:{total:.2f}"[:400],
            )
        if errores:
            avisar(
                fuente="quimicos", nivel="error",
                titulo=f"{len(errores)} compra(s) de químicos no se pudieron cargar",
                detalle=str((errores[0] or {}).get("error") or "")[:140],
                cantidad=len(errores), url="/compras",
                clave=("quimicos:error:"
                       + ",".join(sorted(str((e or {}).get("factura") or "")
                                         for e in errores))[:300]),
            )
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe la carga
        log.warning("puente formulas: no pude avisar a novedades: %s", e)


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo: carga y corrige lo del mes. Nunca levanta.

    Freno propio de 30 min (igual que tejeduría / hilo local). Existe porque
    las facturas del día en curso ahora SÍ se cargan: si formulas las sigue
    creciendo, esta corrida las vuelve a alinear el mismo día, sin esperar al
    cron de la madrugada.
    """
    global _auto_ultimo_ts
    res = {"corrio": False, "creadas": 0, "ajustadas": 0, "importe": 0.0}
    if not autosync_habilitado():
        return res
    ahora = _time.monotonic()
    with _AUTO_LOCK:
        if _auto_ultimo_ts and (ahora - _auto_ultimo_ts) < _AUTO_INTERVALO_MIN:
            return res
        _auto_ultimo_ts = ahora
    try:
        res["corrio"] = True
        rep = sincronizar_mes_actual()
        res["creadas"] = len(rep.get("creadas") or [])
        res["ajustadas"] = len(rep.get("ajustadas") or [])
        res["importe"] = round(
            sum(float(c.get("importe") or 0) for c in (rep.get("creadas") or [])), 2)
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        log.warning("puente formulas (fondo): %s", e)
    return res


def sincronizar_mes_actual(usuario: str = "formulas-auto") -> dict:
    """Hook para el cron diario. Fail-soft total (nunca levanta)."""
    try:
        if not autosync_habilitado():
            return {"disponible": False, "creadas": [], "errores": [],
                    "ajustadas": [], "ya_cargadas": 0, "apagado": True}
        from filters import today_ec
        h = today_ec()
        return sincronizar_mes(h.year, h.month, usuario=usuario)
    except Exception as e:  # noqa: BLE001
        log.exception("sincronizar_mes_actual falló: %s", e)
        return {"disponible": False, "creadas": [], "errores": [{"error": str(e)}],
                "ajustadas": [], "ya_cargadas": 0}
