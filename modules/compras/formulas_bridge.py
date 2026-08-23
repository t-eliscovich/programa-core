"""Puente de compras de colorantes/químicos: formulas_app → Programa Core.

formulas_app registra las compras de químicos al recibirlas (tabla
`public.compras` en la DB `postgres` del mismo RDS: producto, fecha ISO,
proveedor, factura, cantidad, precio_us SIN IVA). Este módulo las trae a
`scintela.compra` vía `queries.crear()` (tipo Q) — que es lo que genera el
pasivo (`scintela.posdat` banc=0) — con:

    - mapping de proveedores formulas → PC (SEY→SY, AVQ→AQ, PRO→PO, NQ→NQ,
      EMP→ES, QSI→QI). Un código que no está en el mapping YA NO FRENA la
      compra: si ese mismo código existe en el maestro de proveedores se usa
      ése, y si no existe se da de alta el proveedor con ese código y ese
      nombre, se carga la compra y la campanita pide completarle el nombre
      real (ver `resolver_prov`). COLO (COLOURTEX) se EXCLUYE: es importación
      y entra por otro circuito (banc=9, con gastos de importación que
      formulas no conoce).
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
# (julio 2026). Es el mapping de los que NO se llaman igual en los dos lados:
# un código nuevo que no esté acá ya no se traba, lo resuelve `resolver_prov`.
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

# Meses hacia atrás y hacia adelante en los que se busca el par de una factura
# de formulas dentro de scintela.compra. Ver _compras_pc_mes.
_VENTANA_MESES = 6

# PISO del barrido histórico. Dueña 2026-07-30: *"enfocate en julio. lo
# anterior no importa. no vamos a mover ni mayo ni junio"* — hay 9 facturas de
# abril–junio por ~$42.100 c/IVA que formulas registró y nunca entraron al
# programa (el puente nació el 17/07 y esos meses dependían del FoxPro). Se
# toman como cerradas: NO se cargan y NO se avisan, porque un aviso que no se
# va a accionar entrena a ignorar la campanita. El barrido sigue vivo de julio
# en adelante, que es para lo que se construyó: que no se vuelva a perder un
# mes entero en silencio.
_HISTORICO_DESDE = (2026, 7)


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
    estado: str                    # cargada | pendiente | excluida | sin_mapear | sin_numero
    detalle_match: str | None = None
    prov_origen: str = "mapeado"      # mapeado | maestro | nuevo | sin_codigo
    prov_nombre_pc: str | None = None  # nombre del proveedor en el maestro
    id_compra_pc: int | None = None   # la compra de PC que la matcheó
    importe_pc: float | None = None   # importe con el que quedó cargada
    ajustable: bool = False           # formulas cambió → hay que corregirla

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fecha"] = self.fecha.isoformat() if self.fecha else None
        return d


# ── Proveedor: del código de formulas al del programa ───────────────────────

def proveedores_pc() -> dict[str, str]:
    """El maestro de proveedores: código → nombre."""
    out: dict[str, str] = {}
    for f in db.fetch_all(
        "SELECT codigo_prov, COALESCE(nombre, '') AS nombre "
        "  FROM scintela.proveedor"
    ):
        cod = (f.get("codigo_prov") or "").strip().upper()
        if cod:
            out.setdefault(cod, (f.get("nombre") or "").strip())
    return out


def resolver_prov(prov_formulas: str, maestro: dict[str, str]) -> tuple[str | None, str]:
    """Código de PC para un proveedor de formulas. Devuelve (código, origen).

    ⭐ TMT 2026-08-23 (dueña, mirando el aviso "Proveedor de químicos sin
    reconocer: NSQ"): *"se debería cargar la compra igual y pedir después
    cargar el proveedor, ponerle el nombre con el que viene"*. Antes, un
    código que no estuviera en `PROV_MAP` dejaba la compra AFUERA — el pasivo
    quedaba de menos hasta que alguien tocara el código. Ahora frenar es la
    excepción, no la regla:

    · `mapeado` — está en `PROV_MAP` (se llaman distinto en cada lado).
    · `maestro` — el mismo código ya existe en scintela.proveedor: es ése
      (es el caso NQ→NQ). El aviso dice bajo cuál quedó, por si no lo fuera.
    · `nuevo`   — no existe en ningún lado y entra en los 3 caracteres de
      `codigo_prov`: se da de alta con ese código y ese nombre, se carga la
      compra, y la campanita pide completarle el nombre real.
    · `sin_codigo` — el código de formulas no entra en 3 caracteres (COLO y
      compañía): no hay forma de elegir el código de PC sin adivinar, así que
      ahí sí se frena y se avisa.
    """
    p = (prov_formulas or "").strip().upper()
    if not p:
        return None, "sin_codigo"
    if p in PROV_MAP:
        return PROV_MAP[p], "mapeado"
    if p in maestro:
        return p, "maestro"
    if len(p) <= 3:
        return p, "nuevo"
    return None, "sin_codigo"


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


def _mes_offset(anio: int, mes: int, delta: int) -> date:
    """Primer día del mes (anio, mes) corrido `delta` meses."""
    m = mes - 1 + delta
    return date(anio + m // 12, m % 12 + 1, 1)


def _compras_pc_mes(anio: int, mes: int) -> list[dict]:
    """Compras de PC contra las que matchear (excluye anuladas stat='Y').

    VENTANA ANCHA (±_VENTANA_MESES), no el mes exacto. La fecha de una compra
    NO coincide entre los dos sistemas: formulas la fecha cuando RECIBE la
    mercadería y el dBase cuando la TIPEAN, y el desfase puede ser de meses.
    Casos reales que lo obligaron (30/07):
      · SEY 21859 — formulas 28/04, PC 01/05 (un mes).
      · SEY 21945 / 21960 / 21981 — formulas 08–12/05, y Andrés las tipeó en
        PC el **16/07**: más de dos meses. Con la ventana angosta salían como
        "sin cargar" y apretar el botón las habría DUPLICADO (y ya están
        pagadas).
    El match sigue siendo por NÚMERO exacto de factura, que es único por
    proveedor, así que abrir la ventana no inventa coincidencias: sólo deja de
    perder las que ya están. No se abre a "todo": los proveedores reinician la
    numeración de vez en cuando (AQ pasó de 6086 a 1 en junio) y una ventana
    acotada evita cruzar dos series distintas.
    """
    ini = _mes_offset(anio, mes, -_VENTANA_MESES)
    fin = _mes_offset(anio, mes, _VENTANA_MESES + 1)
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
    maestro = proveedores_pc()
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
        prov_pc, origen = resolver_prov(prov_f, maestro)
        if not prov_pc:
            filas.append(FilaPuente(
                prov_f, None, factura_f, None, fecha, kg, siva, 0.0, siva,
                "sin_mapear",
                "el código del programa de tintorería no entra en 3 letras: "
                "hay que dar de alta el proveedor a mano",
                prov_origen=origen))
            continue
        prov_nombre = maestro.get(prov_pc)
        if not factura_f:
            # SIN N° DE FACTURA: el puente identifica cada compra por su
            # número — sin él no la puede reconocer en la corrida siguiente y
            # la cargaría DE NUEVO cada vez (con el ciclo de 30 min serían
            # decenas de duplicados por día). Se frena y se avisa.
            iva0 = IVA_POR_PROV.get(prov_pc, IVA_DEFAULT)
            filas.append(FilaPuente(
                prov_f, prov_pc, factura_f, None, fecha, kg, siva, iva0,
                round(siva * (1 + iva0), 2), "sin_numero",
                "la compra no tiene N° de factura en el programa de tintorería",
                prov_origen=origen, prov_nombre_pc=prov_nombre))
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
            prov_origen=origen, prov_nombre_pc=prov_nombre,
            id_compra_pc=(compra_pc or {}).get("id_compra"),
            importe_pc=importe_pc, ajustable=ajustable,
        ))
    pendientes = [f for f in filas if f.estado == "pendiente"]
    ajustables = [f for f in filas if f.ajustable]
    trabadas = [f for f in filas if f.estado in ("sin_mapear", "sin_numero")]
    return {
        "disponible": True,
        "filas": filas,
        "proveedores_nuevos": sorted({f.proveedor_pc for f in filas
                                      if f.prov_origen == "nuevo"
                                      and f.proveedor_pc}),
        "pendientes": len(pendientes),
        "total_pendiente": round(sum(f.importe_con_iva for f in pendientes), 2),
        "ajustables": len(ajustables),
        "trabadas": len(trabadas),
        "total_trabado": round(sum(f.importe_con_iva for f in trabadas), 2),
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
                "ya_cargadas": 0, "ajustadas": [], "proveedores": []}
    creadas, errores, ajustadas, provs_dados_de_alta = [], [], [], []
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
                _previo = (res or {}).get("importe_previo")
                ajustadas.append({
                    "proveedor": f.proveedor_pc, "factura": f.factura_pc,
                    # si `editar` no lo devolvió, el importe que PC tenía
                    # antes de la corrida es el que leímos en estado_mes
                    "importe_previo": (_previo if _previo is not None
                                       else f.importe_pc),
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
        if f.prov_origen == "nuevo" and not _dar_de_alta_prov(
                f, provs_dados_de_alta, errores, usuario):
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
                "prov_origen": f.prov_origen,
                "prov_formulas": f.proveedor_formulas,
                "prov_nombre_pc": f.prov_nombre_pc,
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
    avisar_proveedores(provs_dados_de_alta, creadas)
    avisar_trabadas(est)
    return {
        "disponible": True,
        "creadas": creadas,
        "errores": errores,
        "ajustadas": ajustadas,
        "proveedores": provs_dados_de_alta,
        "ya_cargadas": sum(1 for f in est["filas"] if f.estado == "cargada"),
    }


def _dar_de_alta_prov(f: FilaPuente, dados_de_alta: list, errores: list,
                      usuario: str) -> bool:
    """Da de alta el proveedor nuevo para poder cargarle la compra.

    El nombre es el código con el que viene del programa de tintorería, que es
    lo único que ese programa guarda del proveedor (no tiene maestro: Andrés
    escribe el código a mano). Después la campanita pide completarlo.
    Devuelve False sólo si el alta falló: ahí la compra no se carga y el error
    queda en la lista.
    """
    from modules.proveedores import queries as prov_queries

    cod = f.proveedor_pc or ""
    if any(p.get("codigo") == cod for p in dados_de_alta):
        return True
    try:
        prov_queries.crear(codigo_prov=cod, nombre=cod, usuario=usuario)
        log.info("puente formulas: proveedor nuevo dado de alta %s", cod)
    except ValueError:
        # lo creó otra corrida entremedio: no es un error, se sigue
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("puente formulas: no pude dar de alta el proveedor %s: %s",
                    cod, e)
        errores.append({"proveedor": cod, "factura": f.factura_pc,
                        "error": f"no se pudo dar de alta el proveedor: {e}"})
        return False
    dados_de_alta.append({"codigo": cod,
                          "prov_formulas": f.proveedor_formulas})
    return True


def avisar_proveedores(dados_de_alta: list, creadas: list) -> int:
    """Campanita de los proveedores que el puente resolvió solo. Nunca levanta.

    Dos avisos distintos, porque piden dos cosas distintas:

    · **dado de alta** — el proveedor no existía. La compra YA está cargada
      (con su pasivo) y lo único que falta es ponerle el nombre de verdad,
      que el programa de tintorería no conoce.
    · **cargado bajo uno que ya existía** — el código de tintorería coincidía
      con uno del maestro. Se usó ése, y el aviso dice cuál para poder
      desarmarlo si no era.
    """
    puestos = 0
    try:
        from filters import num_es
        from modules.avisos import avisar

        por_prov: dict = {}
        for c in creadas or []:
            cod = c.get("proveedor") or ""
            acc = por_prov.setdefault(cod, {"n": 0, "importe": 0.0})
            acc["n"] += 1
            acc["importe"] += float(c.get("importe") or 0)

        for p in dados_de_alta or []:
            cod = p.get("codigo") or ""
            acc = por_prov.get(cod) or {"n": 0, "importe": 0.0}
            puestos += bool(avisar(
                fuente="quimicos", nivel="alerta",
                titulo=f"Proveedor de químicos nuevo: {cod}",
                detalle=(f"Vino del programa de tintorería y no estaba acá. "
                         f"Se dio de alta con el código {cod} y se "
                         f"{'cargó' if acc['n'] == 1 else 'cargaron'} "
                         f"{acc['n']} factura{'' if acc['n'] == 1 else 's'} "
                         f"por $ {num_es(acc['importe'], 2)}. "
                         f"Ponele el nombre y los datos."),
                importe=round(acc["importe"], 2), cantidad=acc["n"] or 1,
                url=f"/proveedores/{cod}/editar",
                clave=f"quimicos:prov-nuevo:{cod}"[:400],
            ))

        vistos = {p.get("codigo") for p in dados_de_alta or []}
        de_maestro: dict = {}
        for c in creadas or []:
            if c.get("prov_origen") != "maestro":
                continue
            cod = c.get("proveedor") or ""
            if cod in vistos:
                continue
            acc = de_maestro.setdefault(
                cod, {"n": 0, "importe": 0.0,
                      "nombre": c.get("prov_nombre_pc") or "",
                      "formulas": c.get("prov_formulas") or ""})
            acc["n"] += 1
            acc["importe"] += float(c.get("importe") or 0)
        for cod, acc in sorted(de_maestro.items()):
            puestos += bool(avisar(
                fuente="quimicos", nivel="alerta",
                titulo=f"Químicos: {acc['formulas']} se cargó como {cod}",
                detalle=(f"El programa de tintorería lo llama "
                         f"{acc['formulas']} y acá hay un proveedor con ese "
                         f"mismo código: {acc['nombre'] or cod}. Se le "
                         f"{'cargó' if acc['n'] == 1 else 'cargaron'} "
                         f"{acc['n']} factura{'' if acc['n'] == 1 else 's'} "
                         f"por $ {num_es(acc['importe'], 2)}. Si no era ése, "
                         f"avisá."),
                importe=round(acc["importe"], 2), cantidad=acc["n"],
                url="/compras/desde-formulas",
                clave=f"quimicos:prov-maestro:{cod}:{acc['n']}"[:400],
            ))
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe la carga
        log.warning("puente formulas: no pude avisar los proveedores: %s", e)
    return puestos


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
            # TMT 2026-08-04 (dueña): *"aca poneme de cuanto era el anterior
            # o (+100) algo asi"*. El aviso decía sólo el importe nuevo, así
            # que no se podía saber si la factura creció $2 o $2.000 sin
            # abrir la compra. Ahora dice de cuánto era y cuánto cambió.
            previo = sum(float(a.get("importe_previo") or 0) for a in ajustadas)
            delta = round(total - previo, 2)
            if previo:
                cambio = (f"antes $ {num_es(previo, 2)} → ahora $ "
                          f"{num_es(total, 2)} "
                          f"({'+' if delta >= 0 else '−'}$ "
                          f"{num_es(abs(delta), 2)})")
            else:
                cambio = f"ahora $ {num_es(total, 2)}"
            avisar(
                fuente="quimicos",
                titulo=f"Químicos · se corrigió el importe de {len(ajustadas)} compra"
                       f"{'' if len(ajustadas) == 1 else 's'}"
                       + (f" ({'+' if delta >= 0 else '−'}$ {num_es(abs(delta), 2)})"
                          if previo else ""),
                detalle=(f"La factura cambió en el programa de tintorería: "
                         f"{facturas} · {cambio}"),
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


# ── Revisión histórica (que no se pierda un mes entero otra vez) ────────────

def meses_con_compras() -> list[tuple[int, int]]:
    """Meses (año, mes) en los que formulas registró compras. Más nuevo primero."""
    filas = formulas_db.fetch_all(
        "SELECT DISTINCT substring(fecha::text, 1, 7) AS mes "
        "  FROM compras WHERE fecha IS NOT NULL ORDER BY 1 DESC"
    )
    out = []
    for f in filas:
        try:
            a, m = str(f.get("mes"))[:7].split("-")
            out.append((int(a), int(m)))
        except (ValueError, AttributeError):
            continue
    return out


def estado_historico(hoy: date | None = None) -> dict:
    """Todo lo que formulas registró y NUNCA llegó a PC, mes por mes.

    Existe porque el puente sincroniza SÓLO el mes en curso: arrancó el
    17/07/2026 y abril–junio quedaron dependiendo del FoxPro, que no las
    tipeó — 9 facturas por ~$42.100 que nadie vio hasta que la dueña preguntó
    (30/07). Sin este barrido, un mes entero se puede volver a perder en
    silencio.

    Dos meses quedan afuera a propósito: el **en curso** (ése ya lo muestra la
    pantalla normal) y todo lo **anterior a `_HISTORICO_DESDE`** (decisión de
    la dueña, ver la constante).
    """
    if not formulas_db.disponible():
        return {"disponible": False, "meses": [], "facturas": 0, "importe": 0.0}
    from filters import today_ec

    h = hoy or today_ec()
    meses, n, imp = [], 0, 0.0
    for anio, mes in meses_con_compras():
        if (anio, mes) >= (h.year, h.month) or (anio, mes) < _HISTORICO_DESDE:
            continue
        est = estado_mes(anio, mes)
        faltan = [f for f in (est.get("filas") or [])
                  if f.estado in ("pendiente", "sin_numero", "sin_mapear")]
        if not faltan:
            continue
        total = round(sum(f.importe_con_iva for f in faltan), 2)
        meses.append({"anio": anio, "mes": mes, "mes_str": f"{anio:04d}-{mes:02d}",
                      "filas": faltan, "n": len(faltan), "total": total})
        n += len(faltan)
        imp += total
    return {"disponible": True, "meses": meses, "facturas": n,
            "importe": round(imp, 2)}


def avisar_historico(hist: dict) -> int:
    """Un aviso con lo que quedó sin cargar de meses ANTERIORES. Nunca levanta."""
    if not hist.get("facturas"):
        return 0
    try:
        from filters import num_es
        from modules.avisos import avisar

        meses = ", ".join(m["mes_str"] for m in hist["meses"])
        return bool(avisar(
            fuente="quimicos", nivel="alerta",
            titulo=(f"{hist['facturas']} compra"
                    f"{'' if hist['facturas'] == 1 else 's'} de químicos de "
                    f"meses anteriores sin cargar"),
            detalle=(f"$ {num_es(hist['importe'], 2)} que el programa de "
                     f"tintorería registró y nunca entraron acá ({meses}). "
                     f"El pasivo de esos meses está de menos."),
            importe=hist["importe"], cantidad=hist["facturas"],
            url="/compras/desde-formulas/historico",
            clave=f"quimicos:historico:{meses}:{hist['facturas']}"[:400],
        ))
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe nada
        log.warning("puente formulas: no pude avisar el histórico: %s", e)
        return 0


def avisar_trabadas(est: dict) -> int:
    """Avisa por la campanita las compras de formulas que NO se pueden cargar.

    TMT 2026-07-30 (dueña), después de que la QSI del 28/07 estuviera 2 días
    invisible: *"ídem debería avisar, hay compra de fórmulas no sabemos cuál
    es"* — mismo formato que el aviso de tejedor sin reconocer.

    Dos situaciones, y las dos son ahora la EXCEPCIÓN: un proveedor nuevo ya
    no traba nada (se da de alta solo, ver `resolver_prov`), y lo que queda
    acá es lo que de verdad no se puede resolver sin una persona:

    · **CÓDIGO QUE NO ENTRA** — el código del programa de tintorería tiene más
      de 3 letras y acá los códigos son de 3: elegirlo sería adivinar.
    · **SIN N° DE FACTURA** — la compra se cargó en tintorería sin número. Eso
      se arregla allá, y hasta entonces el puente no la toca (sin número no la
      puede reconocer después y la duplicaría en cada corrida).

    Idempotente por `clave`, que incluye el conteo de facturas: si aparece una
    más del mismo proveedor, vuelve a avisar. Nunca levanta.
    """
    puestos = 0
    try:
        from filters import num_es
        from modules.avisos import avisar

        for estado, titulo, que_hacer in (
            ("sin_mapear",
             "Proveedor de químicos con un código que no entra",
             "en el programa de tintorería tiene más de 3 letras y acá los "
             "códigos son de 3, así que no se puede dar de alta solo. Dalo de "
             "alta a mano y avisá para engancharlo."),
            ("sin_numero",
             "Compra de químicos sin N° de factura",
             "sin el número no se puede cargar (no habría cómo reconocerla "
             "después). Ponele el N° en el programa de tintorería y entra sola."),
        ):
            porprov: dict = {}
            for f in est.get("filas") or []:
                if f.estado != estado:
                    continue
                acc = porprov.setdefault(f.proveedor_formulas,
                                         {"n": 0, "importe": 0.0})
                acc["n"] += 1
                acc["importe"] += float(f.importe_con_iva or 0)
            for prov, acc in sorted(porprov.items()):
                puestos += bool(avisar(
                    fuente="quimicos", nivel="alerta",
                    titulo=f"{titulo}: {prov}",
                    detalle=(f"{acc['n']} factura{'' if acc['n'] == 1 else 's'} "
                             f"por $ {num_es(acc['importe'], 2)} este mes · "
                             f"{que_hacer}"),
                    importe=round(acc["importe"], 2), cantidad=acc["n"],
                    url="/compras/desde-formulas",
                    clave=f"quimicos:{estado}:{prov}:{acc['n']}"[:400],
                ))
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe nada
        log.warning("puente formulas: no pude avisar lo trabado: %s", e)
    return puestos


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
        # Barrido de meses anteriores: si quedó un mes entero sin cargar,
        # que se entere por la campanita y no dentro de tres meses.
        res["historico"] = avisar_historico(estado_historico())
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        log.warning("puente formulas (fondo): %s", e)
    return res


def sincronizar_mes_actual(usuario: str = "formulas-auto") -> dict:
    """Hook para el cron diario. Fail-soft total (nunca levanta)."""
    try:
        if not autosync_habilitado():
            return {"disponible": False, "creadas": [], "errores": [],
                    "ajustadas": [], "proveedores": [], "ya_cargadas": 0,
                    "apagado": True}
        from filters import today_ec
        h = today_ec()
        return sincronizar_mes(h.year, h.month, usuario=usuario)
    except Exception as e:  # noqa: BLE001
        log.exception("sincronizar_mes_actual falló: %s", e)
        return {"disponible": False, "creadas": [], "errores": [{"error": str(e)}],
                "ajustadas": [], "proveedores": [], "ya_cargadas": 0}
