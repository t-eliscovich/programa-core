"""Puente de compras de colorantes/químicos: formulas_app → Programa Core.

formulas_app registra las compras de químicos al recibirlas (tabla
`public.compras` en la DB `postgres` del mismo RDS: producto, fecha ISO,
proveedor, factura, cantidad, precio_us SIN IVA). Este módulo las trae a
`scintela.compra` vía `queries.crear()` (tipo Q) — que es lo que genera el
pasivo (`scintela.posdat` banc=0) — con:

    - mapping de proveedores formulas → PC (SEY→SY, AVQ→AQ, PRO→PO, NQ→NQ,
      EMP→ES). COLO (COLOURTEX) se EXCLUYE: es importación y entra por otro
      circuito (banc=9, con gastos de importación que formulas no conoce).
    - Nº de factura normalizado a la convención del programa: sin ceros a la
      izquierda ('0085' → '85'); SEYQUIN recupera el millar truncado
      ('2444' → '22444'). Concepto en formato dBase: factura + día.
    - IVA por proveedor: 15% default, 0% para ES (sal). El importe cargado
      es el total c/IVA, como carga el dBase.
    - dedup: una compra de formulas NO se carga si ya existe en
      scintela.compra una compra del mismo proveedor+mes con el mismo número
      de factura (token del concepto) o con importe equivalente (la carga
      manual del dBase suele llegar por el sync).

Todo fail-soft: si el pool `formulas_db` no está configurado o la query
rompe, `estado_mes()` devuelve disponible=False y `sincronizar_mes()` no
crea nada. El host nunca se rompe por esto.

El sync automático corre a diario desde scripts/procesa_provisiones_mensual.py
(el cron del Scheduled Task) y a demanda desde /compras/desde-formulas.
Para apagarlo rápido: env FORMULAS_COMPRAS_AUTOSYNC=0.

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
}

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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha"] = self.fecha.isoformat() if self.fecha else None
        return d


# ── Normalización ───────────────────────────────────────────────────────────

def normalizar_factura(prov_pc: str | None, factura: str | None) -> str:
    """Número de factura como lo pone el programa.

    - Sin ceros a la izquierda: '0085' → '85' (convención AQ/PO del dBase).
    - SEYQUIN: en formulas truncan el prefijo del millar ('2444' = 22444
      real). Un número de 4 dígitos se completa con el '2' inicial. (Con 5
      dígitos ya viene completo; con menos de 4 no se puede saber → queda
      como está y el match cae al importe.)
    """
    f = (factura or "").strip().upper()
    if f.isdigit():
        f = f.lstrip("0") or "0"
        if (prov_pc or "") == "SY" and len(f) == 4:
            f = "2" + f
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
        SELECT id_compra, codigo_prov, importe, concepto
          FROM scintela.compra
         WHERE fecha >= %s AND fecha < %s
           AND COALESCE(stat, '') <> 'Y'
        """,
        (ini, fin),
    )


# ── Matching ────────────────────────────────────────────────────────────────

def _buscar_match(compras_pc: list[dict], prov_pc: str, factura_pc: str,
                  siva: float, civa: float) -> str | None:
    """¿Ya existe esta factura en PC? Devuelve el detalle del match o None.

    1. Mismo proveedor + mismo número (primer token del concepto).
    2. Mismo proveedor + número sufijo/prefijo + importe en [s/IVA, c/IVA]
       (cargas manuales con IVA mixto o número escrito distinto).
    3. Mismo proveedor + importe igual al c/IVA o s/IVA calculado (±0.5).
    """
    candidatos = [c for c in compras_pc
                  if (c.get("codigo_prov") or "").strip().upper() == prov_pc]
    for c in candidatos:
        tok = _primer_token(c.get("concepto"))
        if tok and tok == factura_pc:
            return f"factura {tok} (id {c.get('id_compra')})"
    lo = min(siva, civa) - _TOL_ABS
    hi = max(siva, civa) + _TOL_ABS
    for c in candidatos:
        tok = _primer_token(c.get("concepto"))
        imp = float(c.get("importe") or 0)
        if (tok and factura_pc
                and (tok.endswith(factura_pc) or factura_pc.endswith(tok))
                and lo <= imp <= hi):
            return f"factura ~{tok} + importe {imp:.2f} (id {c.get('id_compra')})"
    for c in candidatos:
        imp = float(c.get("importe") or 0)
        if abs(imp - civa) <= _TOL_ABS or abs(imp - siva) <= _TOL_ABS:
            return f"importe {imp:.2f} (id {c.get('id_compra')})"
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
        match = _buscar_match(compras_pc, prov_pc, factura_pc, siva, civa)
        filas.append(FilaPuente(
            prov_f, prov_pc, factura_f, factura_pc, fecha, kg, siva, iva,
            civa, "cargada" if match else "pendiente", match,
        ))
    pendientes = [f for f in filas if f.estado == "pendiente"]
    return {
        "disponible": True,
        "filas": filas,
        "pendientes": len(pendientes),
        "total_pendiente": round(sum(f.importe_con_iva for f in pendientes), 2),
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
    """
    from modules.compras import queries as compras_queries

    est = estado_mes(anio, mes)
    if not est.get("disponible"):
        return {"disponible": False, "creadas": [], "errores": [],
                "ya_cargadas": 0}
    creadas, errores = [], []
    for f in est["filas"]:
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
    return {
        "disponible": True,
        "creadas": creadas,
        "errores": errores,
        "ya_cargadas": sum(1 for f in est["filas"] if f.estado == "cargada"),
    }


def sincronizar_mes_actual(usuario: str = "formulas-auto") -> dict:
    """Hook para el cron diario. Fail-soft total (nunca levanta)."""
    try:
        if not autosync_habilitado():
            return {"disponible": False, "creadas": [], "errores": [],
                    "ya_cargadas": 0, "apagado": True}
        from filters import today_ec
        h = today_ec()
        return sincronizar_mes(h.year, h.month, usuario=usuario)
    except Exception as e:  # noqa: BLE001
        log.exception("sincronizar_mes_actual falló: %s", e)
        return {"disponible": False, "creadas": [], "errores": [{"error": str(e)}],
                "ya_cargadas": 0}
