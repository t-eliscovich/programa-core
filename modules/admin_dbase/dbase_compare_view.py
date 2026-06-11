"""Endpoint /admin/dbase-compare — comparador sistemático PC vs dBase SIN sync.

Pedido dueña 2026-06-10: "construí todos los checks que tenemos que hacer
para ver cómo diferimos del dBase, así es sistemático. No quiero hacer sync.
Quiero poder comparar exacto ya que se están usando los dos programas."

SOLO LECTURA — no escribe nada, nunca. Subís el tarball con los DBF frescos
(el mismo dbf-fresh.tar.gz del sync, pero acá NO se importa nada) y el
reporte calcula cada valor del lado dBase con la regla EXACTA del PRG
(INFORMES.PRG, citada por línea — todas validadas contra pantalla/HISTORIA
el 2026-06-10) y lo compara con el balance live de PC (informe_balance()).

Pedido dueña 2026-06-11: "tenemos que ver 1 a 1 los MOVIMIENTOS, no los
totales" → caja línea por línea, cheques cheque por cheque, anticipos partida
por partida, facturas apareadas por N° SRI, y TOTP as-of la fecha del tarball
(el DBF es de anoche; PC ya acumuló las cuotas YY de hoy → falso +32k).

Secciones:
  [1] Caja            CAJA.DBF último saldo            vs salcaj
      + LÍNEA POR LÍNEA (fecha+|importe| multiset) + saldo fin de día ►
  [2] Bancos          PICHINCH/INTER último saldo      vs saldo PC
      + DETALLE por movimiento (fecha+|importe| multiset): qué movs tiene
      el dBase que PC no (p.ej. los pagos AI del 09/06) y viceversa.
  [3] Cheques  TOTC   STAT $ 'Z123PD' (L24)            vs totc
      + CHEQUE POR CHEQUE (cliente+importe+grupo vivo/no-vivo) con stat
  [4] Facturas TOTF   STAT $ 'ZA' (L27) + buckets del facturas-reconcile
      + pareo por N° SRI (numf_completo de PC vs NUMF del DBF), vivas
  [5] Anticipos       DOLARES ST=' ' (L58)             vs antic
      + PARTIDA POR PARTIDA (cta+importe multiset), solo vivas
  [6] Pasivos  TOTP   POSDAT BANC#9 (L55)              vs totp
      + TOTP de PC AS-OF el mtime de POSDAT.DBF (sin las cuotas YY/RT
        que PC acumuló después del tarball) — ambas cifras a la vista
  [7] Activos         TIPO='I' / TIPO$'MCK' (L47-48)   vs uact/umaq (+sin tipo)
  [8] Retiros         RETIROS mes / año (L37-38)       vs uret/uret_anio
  [9] Producción      KM/VM/KK/KTINT/KR/ITIN/KV del mes vs panel COSTOS
 [10] Stock etapas    HI/TJ/PF + UMX/UKK/UFF + VSTO (L313-345) vs panel STOCK
 [11] Stock químicos  VQX = VQ0+VQQ−ITIN (L322)        vs vqx
 [12] PATANT          HISTORIA mes anterior            vs patant
 [13] UTILIDAD        (TOTL−TOTP)−PATANT (L380)        vs utilidad
      + IDENTIDAD: Δutilidad == Σ Δcomponentes (residuo 0,00 = el reporte
      se explica solo; cualquier residuo = diferencia NO entendida).
"""
from __future__ import annotations

import sys
import tarfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("dbase_compare", __name__, url_prefix="/admin/dbase-compare")

if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\dbase_compare.tar.gz")
    EXTRACT_DIR = Path(r"C:\dbase_compare")
else:
    TARBALL_PATH = Path("/tmp/dbase_compare.tar.gz")
    EXTRACT_DIR = Path("/tmp/dbase_compare")

MAX_TARBALL_BYTES = 120 * 1024 * 1024
DBFS = ["CAJA.DBF", "PICHINCH.DBF", "INTER.DBF", "CHEQUES.DBF", "FACTURAS.DBF",
        "DOLARES.DBF", "POSDAT.DBF", "ACTIVOS.DBF", "RETIROS.DBF",
        "COMPRAS.DBF", "TINTO.DBF", "INICIALE.DBF", "HISTORIA.DBF"]
MAX_LISTADO = 30
MAX_LISTADO_FACT = 40  # facturas por N° SRI: máx 40 líneas por lado


def _f(x) -> float:
    return float(x or 0)


def _hoy_ec() -> date:
    return (datetime.utcnow() - timedelta(hours=5)).date()


def _leer(nombre: str) -> list[dict]:
    import dbfread
    p = EXTRACT_DIR / nombre
    if not p.exists():
        return []
    return list(dbfread.DBF(str(p), char_decode_errors="replace", load=False))


# ───────────────── lado dBase (reglas PRG, validadas 2026-06-10) ─────────────────

def lado_dbase() -> dict:
    """Calcula TODOS los valores del PRG desde los DBF extraídos."""
    hoy = _hoy_ec()
    mes, anio = hoy.month, hoy.year
    d: dict = {"faltantes": [n for n in DBFS if not (EXTRACT_DIR / n).exists()]}

    caja = _leer("CAJA.DBF")
    d["salcaj"] = _f(caja[-1].get("SALDO")) if caja else None
    d["caja_movs"] = [{"fecha": r.get("FECHA"), "tipo": (r.get("TIPO") or "").strip(),
                       "importe": round(_f(r.get("IMPORTE")), 2),
                       "saldo": round(_f(r.get("SALDO")), 2),
                       "concepto": (str(r.get("CONCEPTO") or "")).strip()} for r in caja]

    pich = _leer("PICHINCH.DBF")
    inter = _leer("INTER.DBF")
    d["salbanc1"] = _f(pich[-1].get("SALDO")) if pich else None
    d["salbanc2"] = _f(inter[-1].get("SALDO")) if inter else None
    d["pich_movs"] = [{"fecha": r.get("FECHA"), "doc": (r.get("DOC") or "").strip(),
                       "importe": round(_f(r.get("IMPORTE")), 2),
                       "saldo": round(_f(r.get("SALDO")), 2),
                       "concepto": (r.get("CONCEPTO") or "").strip()} for r in pich]
    d["pich_mtime"] = None
    p = EXTRACT_DIR / "PICHINCH.DBF"
    if p.exists():
        d["pich_mtime"] = datetime.utcfromtimestamp(p.stat().st_mtime) - timedelta(hours=5)

    chq = _leer("CHEQUES.DBF")
    d["totc"] = sum(_f(r.get("IMPORTE")) for r in chq
                    if (r.get("STAT") or "").strip() in ("Z", "1", "2", "3", "P", "D"))
    d["cheq_rows"] = [{"cliente": (str(r.get("CLIENTE") or "")).strip().upper(),
                       "importe": round(_f(r.get("IMPORTE")), 2),
                       "stat": (str(r.get("STAT") or "")).strip().upper(),
                       "fechad": r.get("FECHAD"), "fechout": r.get("FECHOUT")}
                      for r in chq]

    dol = _leer("DOLARES.DBF")
    d["antic"] = sum(_f(r.get("IMPORTE")) for r in dol
                     if (r.get("ST") or " ").strip() == "")
    d["dol_vivos"] = [{"cta": (str(r.get("CTA") or "")).strip().upper(),
                       "importe": round(_f(r.get("IMPORTE")), 2),
                       "fecha": r.get("FECHA"),
                       "concepto": (str(r.get("CONCEPTO") or "")).strip()}
                      for r in dol if (r.get("ST") or " ").strip() == ""]

    d["totp"] = sum(_f(r.get("IMPORTE")) for r in _leer("POSDAT.DBF")
                    if int(r.get("BANC") or 0) != 9)
    pp = EXTRACT_DIR / "POSDAT.DBF"
    d["posdat_mtime"] = (datetime.utcfromtimestamp(pp.stat().st_mtime)
                         - timedelta(hours=5)) if pp.exists() else None

    act = _leer("ACTIVOS.DBF")
    d["uact"] = sum(_f(r.get("VALOR")) for r in act if (r.get("TIPO") or "").strip() == "I")
    d["umaq"] = sum(_f(r.get("VALOR")) for r in act
                    if (r.get("TIPO") or "").strip() in ("M", "C", "K"))
    d["act_sin_tipo"] = sum(_f(r.get("VALOR")) for r in act
                            if (r.get("TIPO") or "").strip() not in ("M", "C", "K", "I"))

    ret = _leer("RETIROS.DBF")
    d["uret"] = sum(_f(r.get("RET")) for r in ret
                    if r.get("FECHA") and r["FECHA"].year == anio and r["FECHA"].month == mes)
    d["uret_anio"] = sum(_f(r.get("RET")) for r in ret
                         if r.get("FECHA") and r["FECHA"].year == anio)

    # ── producción del mes (COMPRAS + TINTO) — PRG L228-267 ──
    comp = [r for r in _leer("COMPRAS.DBF")
            if r.get("FECHA") and r["FECHA"].year == anio and r["FECHA"].month == mes]
    KM = VM = KK = KT_c = VQQ = 0.0
    for r in comp:
        t = (r.get("TIPO") or "").strip()
        if t == "H":
            KM += _f(r.get("KG")); VM += _f(r.get("IMPORTE"))
        elif t == "K":
            KK += _f(r.get("KG"))
        elif t == "T":
            KT_c += _f(r.get("KG"))
        elif t == "Q":
            VQQ += _f(r.get("IMPORTE"))
    tin = _leer("TINTO.DBF")

    def _lav(r):
        return str(r.get("COLOR") or "").strip().upper().startswith("LAV")
    KTINT = sum(_f(r.get("KG")) for r in tin if not _lav(r))
    KR = sum(_f(r.get("KGN")) for r in tin if not _lav(r) and _f(r.get("KG")) > 0)
    KSTI = sum(_f(r.get("KG")) for r in tin if (r.get("STAT") or "").strip() == "S")
    ITIN = sum(_f(r.get("IMPORTE")) for r in tin)
    fact_mes = [r for r in _leer("FACTURAS.DBF")
                if r.get("FECHA") and r["FECHA"].year == anio and r["FECHA"].month == mes]
    KV = sum(_f(r.get("KG")) for r in fact_mes
             if (r.get("STAT") or "").strip() in ("Z", "A", "T", "P")
             and (r.get("TIPO") or "").strip() != "S")
    d["fact_kv_rows"] = [
        {"fecha": r.get("FECHA"), "numf": int(r.get("NUMF") or 0),
         "cli": (str(r.get("CLIENTE") or "")).strip().upper(),
         "stat": (str(r.get("STAT") or "")).strip().upper(),
         "kg": round(_f(r.get("KG")), 2)}
        for r in fact_mes
        if (r.get("STAT") or "").strip() in ("Z", "A", "T", "P")
        and (r.get("TIPO") or "").strip() != "S" and _f(r.get("KG")) != 0]
    d.update(KM=KM, VM=VM, KK=KK, KTINT=KTINT, KR=KR, KSTI=KSTI, ITIN=ITIN,
             KV=KV, VQQ=VQQ, KT_c=KT_c)
    d["compras_mes_rows"] = [
        {"fecha": r.get("FECHA"), "tipo": (str(r.get("TIPO") or "")).strip().upper(),
         "prov": (str(r.get("PROV") or "")).strip().upper(),
         "kg": round(_f(r.get("KG")), 2), "importe": round(_f(r.get("IMPORTE")), 2)}
        for r in comp]
    d["tinto_rows"] = [
        {"fecha": r.get("FECHA"), "cod": (str(r.get("COD") or "")).strip().upper(),
         "kg": round(_f(r.get("KG")), 2), "kgn": round(_f(r.get("KGN")), 2),
         "importe": round(_f(r.get("IMPORTE")), 2),
         "stat": (str(r.get("STAT") or "")).strip().upper()}
        for r in tin]

    # ── stock por etapa (PRG L304-345) — fila ANTERIOR de INICIALE ──
    ini = _leer("INICIALE.DBF")
    if len(ini) >= 2:
        prev = ini[-2]
        HI0, TJ0, PF0 = _f(prev.get("HILADO")), _f(prev.get("TEJIDO")), _f(prev.get("TERMINADO"))
        UM0, VQ0 = _f(prev.get("UM")), _f(prev.get("VQ"))
        KT = KT_c + KTINT - KSTI
        KH = KK / 0.995  # DESK = 0.5 (PRG L6)
        HI = HI0 + KM - KH
        TJ = max(0.0, TJ0 + KK - KT)
        PF = PF0 + KR - KV
        UMX = (VM + (HI - KM) * UM0) / HI if HI > 0 else UM0
        UKK, UFF = UMX + .5, UMX + 2.2
        d.update(HI=HI, TJ=TJ, PF=PF, UMX=UMX, UKK=UKK, UFF=UFF,
                 VSTO=HI * UMX + TJ * UKK + PF * UFF,
                 VQX=VQ0 + VQQ - ITIN, HI0=HI0, TJ0=TJ0, PF0=PF0,
                 UM0=UM0, VQ0=VQ0)

    # ── PATANT: HISTORIA fila del mes anterior (PRG L281-284) ──
    mm = 12 if mes == 1 else mes - 1
    yy = anio - 1 if mes == 1 else anio
    for r in _leer("HISTORIA.DBF"):
        f = r.get("FECHA")
        if f and f.year == yy and f.month == mm:
            d["patant"] = _f(r.get("PATRIMONIO"))
            d["hist_prev"] = {k.lower(): _f(r.get(k)) for k in
                              ("STOCK", "USTOCK", "UQUI", "CART", "BANCO", "DEUDA", "USUTI")}
    return d


def utilidad_dbase(d: dict, totf_dbf: float) -> float | None:
    """UTILIDAD del PRG (L370-380): TOTL − TOTP − PATANT, TOTL incluye URET mes."""
    req = ("salcaj", "salbanc1", "VSTO", "VQX", "patant")
    if any(d.get(k) is None for k in req):
        return None
    salbanc = _f(d["salbanc1"]) + _f(d.get("salbanc2"))
    cart = totf_dbf + d["totc"]
    totl = (salbanc + d["salcaj"] + cart + d["VSTO"] + d["VQX"]
            + d["umaq"] + d["uact"] + d["uret"] + d["antic"])
    return totl - d["totp"] - d["patant"]


# ───────────────── lado PC ─────────────────

def _pc_movs_pichincha(desde: date) -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT t.fecha, COALESCE(t.documento, '') AS doc, t.importe,
               t.saldo, COALESCE(t.concepto, '') AS concepto
          FROM scintela.transacciones_bancarias t
          JOIN scintela.banco b ON b.no_banco = t.no_banco
         WHERE UPPER(b.nombre) LIKE 'PICHINCH%%'
           AND t.fecha >= %s
         ORDER BY t.fecha, t.id_transaccion
        """,
        (desde,),
    ) or []
    return [dict(r) for r in rows]


# ───────────────── comparación de movimientos de banco ─────────────────

def diff_movs_banco(dbf_movs: list[dict], pc_movs: list[dict], desde: date) -> dict:
    """Multiset por (fecha, |importe|). Devuelve {solo_dbase, solo_pc}."""
    def key(m):
        return (str(m.get("fecha") or ""), round(abs(_f(m.get("importe"))), 2))
    db_n, pc_n = defaultdict(list), defaultdict(list)
    for m in dbf_movs:
        if m.get("fecha") and m["fecha"] >= desde:
            db_n[key(m)].append(m)
    for m in pc_movs:
        if m.get("fecha") and m["fecha"] >= desde:
            pc_n[key(m)].append(m)
    solo_db, solo_pc = [], []
    for k in set(db_n) | set(pc_n):
        a, b = db_n.get(k, []), pc_n.get(k, [])
        n = min(len(a), len(b))
        solo_db.extend(a[n:])
        solo_pc.extend(b[n:])
    return {"solo_dbase": solo_db, "solo_pc": solo_pc}



def saldos_fin_de_dia(dbf_movs: list[dict], pc_movs: list[dict], desde: date) -> list[dict]:
    """Saldo de la ULTIMA fila de cada dia, dBase vs PC, con el delta.

    Pedido duena 2026-06-10 ('ayer estabamos igual - inventos no'): en vez
    de hipotesis sobre el gap de bancos, mostrar dia por dia donde se abre.
    El dia en que delta CAMBIA respecto del dia anterior = ahi esta la causa
    (movimiento faltante de un lado, o saldo re-derivado/pisado).
    """
    db_dia: dict = {}
    for m in dbf_movs:
        f = m.get("fecha")
        if f and f >= desde:
            db_dia[f] = m.get("saldo")  # file order: la ultima gana
    pc_dia: dict = {}
    for m in pc_movs:  # ya viene ORDER BY fecha, id
        f = m.get("fecha")
        if f and f >= desde:
            pc_dia[f] = _f(m.get("saldo"))
    out = []
    prev_delta = None
    for f in sorted(set(db_dia) | set(pc_dia)):
        a, b = db_dia.get(f), pc_dia.get(f)
        delta = (b - a) if (a is not None and b is not None) else None
        salto = (delta is not None and prev_delta is not None
                 and abs(delta - prev_delta) > 0.01)
        out.append({"fecha": f, "dbase": a, "pc": b, "delta": delta, "salto": salto})
        if delta is not None:
            prev_delta = delta
    return out

# ───────────────── diffs 1 a 1 (pedido dueña 2026-06-11) ─────────────────
# "Tenemos que ver 1 a 1 los MOVIMIENTOS, no los totales."
# Todos multiset SOLO LECTURA: cancelan 1 a 1 por clave y listan sobrantes.

def _diff_multiset(dbase_rows: list[dict], pc_rows: list[dict], key) -> dict:
    """Multiset genérico: cancela 1 a 1 por `key`; devuelve los sobrantes."""
    a_n, b_n = defaultdict(list), defaultdict(list)
    for r in dbase_rows:
        a_n[key(r)].append(r)
    for r in pc_rows:
        b_n[key(r)].append(r)
    solo_db, solo_pc = [], []
    for k in set(a_n) | set(b_n):
        a, b = a_n.get(k, []), b_n.get(k, [])
        n = min(len(a), len(b))
        solo_db.extend(a[n:])
        solo_pc.extend(b[n:])
    return {"solo_dbase": solo_db, "solo_pc": solo_pc}


_STATS_VIVOS_CHEQUE = ("Z", "1", "2", "3", "P", "D")


def _grupo_stat_cheque(stat) -> str:
    """Grupo del multiset: VIVO (TOTC, PRG L24: STAT $ 'Z123PD') o NO-VIVO."""
    return "VIVO" if (stat or "").strip().upper() in _STATS_VIVOS_CHEQUE else "NO-VIVO"


def diff_cheques(dbf_rows: list[dict], pc_rows: list[dict]) -> dict:
    """Cheque x cheque: multiset por (cliente, importe, grupo vivo/no-vivo).

    Un cheque vivo en dBase pero depositado en PC (o viceversa) aparece en
    AMBAS listas con su stat de cada lado — ese flip es lo que hay que mirar.
    """
    def key(r):
        return ((r.get("cliente") or "").strip().upper(),
                round(_f(r.get("importe")), 2),
                _grupo_stat_cheque(r.get("stat")))
    return _diff_multiset(dbf_rows, pc_rows, key)


def diff_anticipos(dbf_rows: list[dict], pc_rows: list[dict]) -> dict:
    """Partida x partida de anticipos VIVOS: multiset por (cta, importe)."""
    def key(r):
        return ((r.get("cta") or "").strip().upper(),
                round(_f(r.get("importe")), 2))
    return _diff_multiset(dbf_rows, pc_rows, key)


def _numf_sri_pc(r) -> int:
    """N° SRI de una factura PC: últimos dígitos de numf_completo
    ('001-002-000174007' → 174007). Sin numf_completo cae a numf —
    también sirve como clave de las filas DBF (NUMF ya es el N° SRI)."""
    import re
    m = re.search(r"(\d+)$", (r.get("numf_completo") or "").strip())
    if m:
        return int(m.group(1))
    return int(r.get("numf") or 0)


def diff_facturas_sri(dbf_rows: list[dict], pc_rows: list[dict]) -> dict:
    """Facturas VIVAS apareadas por N° SRI (multiset por numf).

    dBase: STAT $ 'ZA' (PRG L27) y NUMF>0. PC: vivas QUE CUENTAN
    (usuario_crea <> 'asinfo-backfill' — criterio dueña: solo lo cargado).
    Las NUMF=0 del dBase (entradas sin numerar) no se pueden aparear por
    SRI: van aparte en `dbase_sin_numf`.
    """
    def _viva_f(stat):
        return (stat or "").strip().upper() in ("", "Z", "A")
    db_vivas = [r for r in dbf_rows if _viva_f(r.get("stat"))]
    db_con_numf = [r for r in db_vivas if int(r.get("numf") or 0) > 0]
    pc_vivas = [r for r in pc_rows if _viva_f(r.get("stat"))
                and (r.get("usuario_crea") or "").strip() != "asinfo-backfill"
                and _numf_sri_pc(r) > 0]
    res = _diff_multiset(db_con_numf, pc_vivas, _numf_sri_pc)
    res["dbase_sin_numf"] = [r for r in db_vivas if int(r.get("numf") or 0) <= 0]
    return res


def ajuste_yy_a_fecha(rows: list[dict], fecha_corte: date) -> float:
    """Δ a sumar al TOTP de PC para expresarlo AS-OF `fecha_corte`.

    `rows` ya pasaron por _resolver_cuotas (traen cuota_diaria) y su
    baseline_date es la fecha hasta la cual el importe persistido acumuló
    cuotas. baseline > corte → restar las cuotas de los días hábiles
    posteriores al corte (PC adelantado, el caso del falso +32k);
    baseline < corte → sumarlas (persist atrasado).
    """
    from modules.posdat.queries import _dias_habiles_entre
    tot = 0.0
    for r in rows:
        cd = float(r.get("cuota_diaria") or 0)
        b = r.get("baseline_date")
        if cd <= 0 or not b:
            continue
        if b > fecha_corte:
            tot -= cd * _dias_habiles_entre(fecha_corte, b)
        elif b < fecha_corte:
            tot += cd * _dias_habiles_entre(b, fecha_corte)
    return round(tot, 2)


# ───────────────── lado PC de los diffs 1 a 1 (SELECTs, solo lectura) ─────────────────

def _pc_movs_caja(desde: date) -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT fecha, COALESCE(tipo, '') AS tipo, importe, saldo,
               COALESCE(concepto, '') AS concepto
          FROM scintela.caja
         WHERE fecha >= %s
         ORDER BY fecha, id_caja
        """,
        (desde,),
    ) or []
    return [dict(r) for r in rows]


def _pc_cheques() -> list[dict]:
    """Todos los cheques PC menos asinfo-backfill (mismo universo que totc)."""
    import db
    rows = db.fetch_all(
        """
        SELECT COALESCE(codigo_cli, '') AS cliente, importe,
               COALESCE(stat, '') AS stat, fechad, fechaout AS fechout,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.cheque
         WHERE COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
    ) or []
    return [dict(r) for r in rows]


def _pc_anticipos_vivos() -> list[dict]:
    """Anticipos vivos PC (st NULL/''/' '), mismo universo que anticipos()."""
    import db
    rows = db.fetch_all(
        """
        SELECT COALESCE(cta, '') AS cta, importe, fecha,
               COALESCE(concepto, '') AS concepto
          FROM scintela.dolares
         WHERE (st IS NULL OR st IN ('', ' '))
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """
    ) or []
    return [dict(r) for r in rows]


def _pc_facturas_sri() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT numf, numf_completo, codigo_cli, fecha, importe, saldo,
               COALESCE(stat, '') AS stat,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.factura
        """
    ) or []
    return [dict(r) for r in rows]


def _pc_posdat_yy_con_cuotas() -> list[dict]:
    """Filas YY/RT vivas de PC con cuota_diaria resuelta por el MISMO motor
    que el display y el persist (modules.posdat.queries._resolver_cuotas)."""
    import db
    from modules.posdat.queries import POSDAT_NO_ANULADA_WHERE, _resolver_cuotas
    rows = db.fetch_all(
        f"""
        SELECT id_posdat, prov, concepto, importe, baseline_date
          FROM scintela.posdat
         WHERE UPPER(TRIM(prov)) IN ('YY', 'RT')
           AND baseline_date IS NOT NULL
           AND COALESCE(banc, 0) = 0
           AND {POSDAT_NO_ANULADA_WHERE}
        """
    ) or []
    rows = [dict(r) for r in rows]
    _resolver_cuotas(rows)
    return rows


# ───────────────── reporte ─────────────────

def _pc_tinto_mes() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT fecha, UPPER(TRIM(COALESCE(cod,''))) AS cod,
               COALESCE(kg,0) AS kg, COALESCE(kgn,0) AS kgn,
               COALESCE(importe,0) AS importe,
               UPPER(TRIM(COALESCE(stat,''))) AS stat,
               COALESCE(usuario_crea,'') AS usuario
          FROM scintela.tinto
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        """) or []
    return [dict(r) for r in rows]


def _pc_compras_mes() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT fecha, UPPER(TRIM(COALESCE(tipo,''))) AS tipo,
               UPPER(TRIM(COALESCE(codigo_prov,''))) AS prov,
               COALESCE(kg,0) AS kg, COALESCE(importe,0) AS importe,
               UPPER(TRIM(COALESCE(stat,''))) AS stat,
               COALESCE(usuario_crea,'') AS usuario
          FROM scintela.compra
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        """) or []
    return [dict(r) for r in rows]


def _pc_facturas_kg_mes() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT fecha, COALESCE(numf, 0) AS numf,
               UPPER(TRIM(COALESCE(codigo_cli,''))) AS cli,
               UPPER(TRIM(COALESCE(stat,''))) AS stat,
               COALESCE(kg,0) AS kg,
               COALESCE(usuario_crea,'') AS usuario
          FROM scintela.factura
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND fecha <  date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
           AND (stat IS NULL OR stat <> 'X')
           AND COALESCE(kg, 0) <> 0
        """) or []
    return [dict(r) for r in rows]


def _pc_iniciales_prev() -> dict:
    import db
    hoy = _hoy_ec()
    mm = 12 if hoy.month == 1 else hoy.month - 1
    yy = hoy.year - 1 if hoy.month == 1 else hoy.year
    row = db.fetch_one(
        "SELECT hilado, tejido, terminado, um, vq FROM scintela.iniciales "
        "WHERE yy = %s AND mesnum = %s ORDER BY id_iniciales DESC LIMIT 1",
        (yy, mm)) or {}
    return {k: _f(row.get(k)) for k in ("hilado", "tejido", "terminado", "um", "vq")}


def _linea_cmp(label: str, db_v, pc_v, tol: float = 0.01) -> str:
    if db_v is None:
        return f"{label:24} dBase=?            PC={pc_v:>14,.2f}   [sin DBF]\n"
    diff = pc_v - db_v
    mark = "✓" if abs(diff) <= tol else "✗"
    return (f"{label:24} dBase={db_v:>14,.2f}  PC={pc_v:>14,.2f}  "
            f"Δ={diff:>+12,.2f}  {mark}\n")


def reporte(dias_banco: int = 30):
    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== dBase-compare — {_hoy_ec()} — SOLO LECTURA (sin sync, sin escribir) ===")
    d = lado_dbase()
    if d.get("faltantes"):
        yield line(f"⚠ DBF faltantes en el tarball: {', '.join(d['faltantes'])}")
    if d.get("pich_mtime"):
        yield line(f"PICHINCH.DBF fechado {d['pich_mtime']:%Y-%m-%d %H:%M} (hora EC) — "
                   "los movimientos posteriores en PC son esperables")
    yield line()

    from modules.admin_dbase.facturas_reconcile_view import (
        _saldo_za, reconciliar_facturas_plan, _leer_pc as _leer_fact_pc, _map_factura_real,
    )
    import dbfread
    mapper = _map_factura_real()
    fact_dbf = []
    fpath = EXTRACT_DIR / "FACTURAS.DBF"
    if fpath.exists():
        for rec in dbfread.DBF(str(fpath), char_decode_errors="replace", load=False):
            row = mapper(rec)
            if row is not None:
                fact_dbf.append(row)
    totf_dbf = round(sum(_saldo_za(r) for r in fact_dbf), 2) if fact_dbf else None

    from modules.informes import queries as iq
    pc = iq.informe_balance()
    deltas: list[tuple[str, float]] = []
    desde = _hoy_ec() - timedelta(days=dias_banco)

    def cmp_acum(label, db_v, pc_v, activo=True):
        nonlocal deltas
        if db_v is not None:
            deltas.append((label, (pc_v - db_v) * (1 if activo else -1)))
        return _linea_cmp(label, db_v, pc_v)

    yield line("── [1] CAJA (CAJA.DBF último saldo) ──")
    yield cmp_acum("Caja", d.get("salcaj"), _f(pc.get("salcaj")))
    try:
        pc_caja = _pc_movs_caja(desde)
        cdiff = diff_movs_banco(d.get("caja_movs") or [], pc_caja, desde)
        yield line(f"  línea por línea desde {desde} — SOLO dBASE (PC no las tiene): "
                   f"{len(cdiff['solo_dbase'])}")
        for m in sorted(cdiff["solo_dbase"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {m['tipo']:2} {m['importe']:>12,.2f}  {m['concepto'][:34]}")
        yield line(f"  línea por línea desde {desde} — SOLO PC (dBase no las tiene): "
                   f"{len(cdiff['solo_pc'])}")
        for m in sorted(cdiff["solo_pc"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {str(m['tipo'])[:2]:2} {_f(m['importe']):>12,.2f}  "
                       f"{str(m['concepto'])[:34]}")
        dias_c = saldos_fin_de_dia(d.get("caja_movs") or [], pc_caja, desde)
        yield line("  saldo CAJA fin de día (dBase vs PC) — ► marca el día donde el gap CAMBIA:")
        for x in dias_c[-MAX_LISTADO:]:
            a = f"{x['dbase']:>14,.2f}" if x["dbase"] is not None else f"{'—':>14}"
            b = f"{x['pc']:>14,.2f}" if x["pc"] is not None else f"{'—':>14}"
            dl = f"{x['delta']:>+12,.2f}" if x["delta"] is not None else f"{'—':>12}"
            yield line(f"    {'►' if x['salto'] else ' '} {x['fecha']} dBase={a}  PC={b}  Δ={dl}")
        saltos_c = [x for x in dias_c if x["salto"]]
        if saltos_c:
            yield line(f"  ⇒ el gap de caja CAMBIA en: {', '.join(str(x['fecha']) for x in saltos_c)}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle caja no disponible: {exc!r}]")
    yield line()

    yield line("── [2] BANCOS (último saldo del DBF vs PC) ──")
    pc_b1 = next((_f(b.get("saldo")) for b in pc.get("bancos_todos") or []
                  if "PICHINCH" in str(b.get("nombre") or "").upper()), _f(pc.get("salbanc")))
    yield cmp_acum("Pichincha", d.get("salbanc1"), pc_b1)
    if d.get("salbanc2") is not None:
        pc_b2 = next((_f(b.get("saldo")) for b in pc.get("bancos_todos") or []
                      if "INTER" in str(b.get("nombre") or "").upper()), 0.0)
        yield cmp_acum("Internacional", d.get("salbanc2"), pc_b2)
    try:
        pcm = _pc_movs_pichincha(desde)
        movdiff = diff_movs_banco(d.get("pich_movs") or [], pcm, desde)
        yield line(f"  movimientos desde {desde} — SOLO dBASE (PC no los tiene): "
                   f"{len(movdiff['solo_dbase'])}")
        for m in sorted(movdiff["solo_dbase"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {m['doc']:3} {m['importe']:>12,.2f}  {m['concepto'][:34]}")
        yield line(f"  movimientos desde {desde} — SOLO PC (dBase no los tiene): "
                   f"{len(movdiff['solo_pc'])}")
        for m in sorted(movdiff["solo_pc"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {str(m['doc'])[:3]:3} {_f(m['importe']):>12,.2f}  "
                       f"{str(m['concepto'])[:34]}")
        dias_t = saldos_fin_de_dia(d.get("pich_movs") or [], pcm, desde)
        yield line("  saldo FIN DE DIA (dBase vs PC) — ► marca el dia donde el gap CAMBIA:")
        for x in dias_t[-MAX_LISTADO:]:
            a = f"{x['dbase']:>14,.2f}" if x["dbase"] is not None else f"{'—':>14}"
            b = f"{x['pc']:>14,.2f}" if x["pc"] is not None else f"{'—':>14}"
            dl = f"{x['delta']:>+12,.2f}" if x["delta"] is not None else f"{'—':>12}"
            yield line(f"    {'►' if x['salto'] else ' '} {x['fecha']} dBase={a}  PC={b}  Δ={dl}")
        saltos = [x for x in dias_t if x["salto"]]
        if saltos:
            yield line(f"  ⇒ el gap CAMBIA en: {', '.join(str(x['fecha']) for x in saltos)}")
        elif dias_t and dias_t[0].get("delta") is not None:
            yield line(f"  ⇒ el gap es CONSTANTE en toda la ventana ({dias_t[0]['delta']:+,.2f}) — "
                       "se origina antes: ampliá los días o revisar recompute de saldos")

        # ── COSTURA DE APERTURA + ORIGEN (causa raíz, pedido dueña) ──
        # El archivo mensual del dBase abre con el carry del cierre anterior.
        # Acá mostramos esa costura y de DÓNDE salieron las filas de PC del
        # mes (usuario_crea + cuándo se crearon) — si la apertura difiere,
        # la causa está en la cola del mes anterior de PC o en un recompute.
        import db as _db
        pm = d.get("pich_movs") or []
        if pm:
            f0 = min(m["fecha"] for m in pm if m.get("fecha"))
            primera = next(m for m in pm if m.get("fecha") == f0)
            yield line(f"  COSTURA: dBase abre {f0} (1ra fila: {primera['doc']} "
                       f"{primera['importe']:+,.2f} → saldo {primera['saldo']:,.2f})")
            cola = _db.fetch_one(
                """
                SELECT t.fecha, t.saldo, COALESCE(t.usuario_crea,'NULL') AS uc,
                       t.fecha_crea
                  FROM scintela.transacciones_bancarias t
                  JOIN scintela.banco b ON b.no_banco = t.no_banco
                 WHERE UPPER(b.nombre) LIKE 'PICHINCH%%' AND t.fecha < %s
                 ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1
                """, (f0,),
            )
            if cola:
                yield line(f"           PC cierra el mes anterior en {cola['fecha']} "
                           f"saldo {_f(cola['saldo']):,.2f} "
                           f"[{cola['uc']}, creada {cola['fecha_crea']}]")
            origen = _db.fetch_all(
                """
                SELECT COALESCE(t.usuario_crea,'NULL') AS uc, COUNT(*) AS n,
                       MIN(t.fecha_crea) AS desde_c, MAX(t.fecha_crea) AS hasta_c
                  FROM scintela.transacciones_bancarias t
                  JOIN scintela.banco b ON b.no_banco = t.no_banco
                 WHERE UPPER(b.nombre) LIKE 'PICHINCH%%' AND t.fecha >= %s
                 GROUP BY 1 ORDER BY 2 DESC
                """, (f0,),
            ) or []
            yield line("  ORIGEN de las filas PC del mes (usuario_crea · n · creadas entre):")
            for o in origen:
                yield line(f"    {o['uc']:<18} {o['n']:>4}  {o['desde_c']} → {o['hasta_c']}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle movimientos no disponible: {exc!r}]")
    yield line()

    yield line("── [3] CHEQUES — TOTC = STAT $ 'Z123PD' (PRG L24) ──")
    yield cmp_acum("Cheques (TOTC)", d.get("totc"), _f(pc.get("totc")))
    try:
        chd = diff_cheques(d.get("cheq_rows") or [], _pc_cheques())
        yield line("  cheque x cheque (cliente+importe+grupo VIVO 'Z123PD'/NO-VIVO):")
        yield line(f"  SOLO dBASE (PC no tiene ese cheque en ese grupo): {len(chd['solo_dbase'])}")
        for c in sorted(chd["solo_dbase"], key=lambda x: str(x.get("fechad")))[-MAX_LISTADO:]:
            yield line(f"    fd={str(c.get('fechad') or ''):10} {c['cliente']:5} "
                       f"{_f(c['importe']):>11,.2f} stat={(c.get('stat') or '·'):2} "
                       f"[{_grupo_stat_cheque(c.get('stat'))}]")
        yield line(f"  SOLO PC (dBase no lo tiene en ese grupo): {len(chd['solo_pc'])}")
        for c in sorted(chd["solo_pc"], key=lambda x: str(x.get("fechad")))[-MAX_LISTADO:]:
            yield line(f"    fd={str(c.get('fechad') or ''):10} {c['cliente']:5} "
                       f"{_f(c['importe']):>11,.2f} stat={(c.get('stat') or '·'):2} "
                       f"[{_grupo_stat_cheque(c.get('stat'))}] "
                       f"[{(c.get('usuario_crea') or 'NULL')[:14]}]")
        yield line("  (mismo cheque en ambas listas = FLIP de stat: vivo de un lado,")
        yield line("   depositado/terminal del otro — ahí está la diferencia de TOTC)")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle cheques no disponible: {exc!r}]")
    yield line()

    yield line("── [4] FACTURAS — TOTF = STAT $ 'ZA' (PRG L27) ──")
    yield cmp_acum("Facturas (TOTF)", totf_dbf, _f(pc.get("totf")))
    if fact_dbf:
        plan = reconciliar_facturas_plan(fact_dbf, _leer_fact_pc())
        s = {k: round(sum(_saldo_za(r) for r in plan[k]), 2)
             for k in ("solo_dbase", "solo_pc_backfill", "solo_pc_carga",
                       "solo_pc_directa", "solo_pc_dbf_huerfana")}
        dz = round(sum(x["delta_za"] for x in plan["diffs"]), 2)
        yield line(f"  buckets: pendiente-sync {len(plan['solo_dbase'])} ({s['solo_dbase']:,.2f}) · "
                   f"backfill {len(plan['solo_pc_backfill'])} ({s['solo_pc_backfill']:,.2f}, no cuenta) · "
                   f"cargadas {len(plan['solo_pc_carga'])} ({s['solo_pc_carga']:,.2f})")
        yield line(f"           directas-PC {len(plan['solo_pc_directa'])} ({s['solo_pc_directa']:,.2f}) · "
                   f"huérfanas {len(plan['solo_pc_dbf_huerfana'])} ({s['solo_pc_dbf_huerfana']:,.2f}) · "
                   f"Δcobranza {len(plan['diffs'])} ({dz:,.2f})")
        yield line("  (detalle completo: /admin/facturas-reconcile)")
        try:
            sri = diff_facturas_sri(fact_dbf, _pc_facturas_sri())
            s_db = round(sum(_saldo_za(r) for r in sri["solo_dbase"]), 2)
            s_pc = round(sum(_saldo_za(r) for r in sri["solo_pc"]), 2)
            yield line("  pareo por N° SRI (vivas, NUMF dBase vs numf_completo PC):")
            yield line(f"  vivas dBASE sin par en PC: {len(sri['solo_dbase'])} ({s_db:,.2f})")
            for r in sorted(sri["solo_dbase"],
                            key=lambda x: int(x.get("numf") or 0))[-MAX_LISTADO_FACT:]:
                yield line(f"    {str(r.get('fecha') or ''):10} numf={int(r.get('numf') or 0):<7} "
                           f"{(r.get('codigo_cli') or '').strip():5} "
                           f"stat={(r.get('stat') or '').strip() or '·':2} "
                           f"saldo={_f(r.get('saldo')):>11,.2f}")
            yield line(f"  vivas PC (que cuentan, sin backfill) sin par en dBase: "
                       f"{len(sri['solo_pc'])} ({s_pc:,.2f})")
            for r in sorted(sri["solo_pc"], key=_numf_sri_pc)[-MAX_LISTADO_FACT:]:
                yield line(f"    {str(r.get('fecha') or ''):10} numf={_numf_sri_pc(r):<7} "
                           f"{(r.get('codigo_cli') or '').strip():5} "
                           f"stat={(r.get('stat') or '').strip() or '·':2} "
                           f"saldo={_f(r.get('saldo')):>11,.2f}  "
                           f"[{(r.get('usuario_crea') or 'NULL')[:16]}]")
            if sri["dbase_sin_numf"]:
                s0 = round(sum(_saldo_za(r) for r in sri["dbase_sin_numf"]), 2)
                yield line(f"  (+{len(sri['dbase_sin_numf'])} vivas dBase con NUMF=0 — sin N° SRI, "
                           f"no apareables por numf; saldo {s0:,.2f})")
        except Exception as exc:  # noqa: BLE001
            yield line(f"  [pareo SRI no disponible: {exc!r}]")
    yield line()

    yield line("── [5] ANTICIPOS — DOLARES ST=' ' (PRG L58) ──")
    yield cmp_acum("Anticipos", d.get("antic"), _f(pc.get("antic")))
    try:
        ad = diff_anticipos(d.get("dol_vivos") or [], _pc_anticipos_vivos())
        yield line("  partida x partida VIVAS (cta+importe multiset):")
        yield line(f"  SOLO dBASE (PC no la tiene viva): {len(ad['solo_dbase'])}")
        for a in sorted(ad["solo_dbase"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {str(a.get('fecha') or ''):10} {a['cta']:3} "
                       f"{_f(a['importe']):>12,.2f}  {str(a.get('concepto') or '')[:30]}")
        yield line(f"  SOLO PC (dBase no la tiene viva): {len(ad['solo_pc'])}")
        for a in sorted(ad["solo_pc"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {str(a.get('fecha') or ''):10} {a['cta']:3} "
                       f"{_f(a['importe']):>12,.2f}  {str(a.get('concepto') or '')[:30]}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle anticipos no disponible: {exc!r}]")
    yield line()

    yield line("── [6] PASIVOS — TOTP = POSDAT BANC#9 (PRG L55) ──")
    yield cmp_acum("Pasivos (TOTP)", d.get("totp"), _f(pc.get("totp")), activo=False)
    try:
        if d.get("posdat_mtime") and d.get("totp") is not None:
            fcorte = d["posdat_mtime"].date()
            adj = ajuste_yy_a_fecha(_pc_posdat_yy_con_cuotas(), fcorte)
            totp_pc_asof = round(_f(pc.get("totp")) + adj, 2)
            yield line(f"  POSDAT.DBF fechado {d['posdat_mtime']:%Y-%m-%d %H:%M} (hora EC) — "
                       f"cuotas YY/RT que PC acumuló DESPUÉS del tarball: {-adj:,.2f}")
            yield _linea_cmp(f"Pasivos as-of {fcorte}", d.get("totp"), totp_pc_asof)
            yield line("  (la línea de arriba compara HOY — el dBase del tarball no tiene las")
            yield line("   cuotas YY de hoy; as-of tarball es la comparación justa. Ambas a la vista.)")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [as-of tarball no disponible: {exc!r}]")
    yield line()

    yield line("── [7] ACTIVOS FIJOS (PRG L47-48) ──")
    yield cmp_acum("Terr/Edif (UACT)", d.get("uact"), _f(pc.get("uact")))
    yield cmp_acum("Maq/Equip (UMAQ)", d.get("umaq"), _f(pc.get("umaq")))
    if d.get("act_sin_tipo"):
        yield line(f"  ⚠ activos SIN TIPO en dBase por {d['act_sin_tipo']:,.0f} — "
                   "ningún sistema los suma (pendiente decisión)")
    yield line()

    yield line("── [8] RETIROS (PRG L37-38) ──")
    yield cmp_acum("Retiros mes (URET)", d.get("uret"), _f(pc.get("uret")))
    yield _linea_cmp("Retiros año", d.get("uret_anio"), _f(pc.get("uret_anio")))
    yield line()

    yield line("── [9] PRODUCCIÓN DEL MES (COMPRAS + TINTO, PRG L228-267) ──")
    res = pc.get("resultados") or {}
    tabla = {r.get("label"): r for r in (res.get("costos") or [])}

    def _kg(lbl):
        return _f((tabla.get(lbl) or {}).get("kg"))
    yield _linea_cmp("KM compras hilado kg", d.get("KM"), _kg("MAT.PR."), tol=1)
    yield _linea_cmp("KK tejido kg", d.get("KK"), _kg("TEJIDO"), tol=1)
    yield _linea_cmp("KTINT tinturado kg", d.get("KTINT"), _kg("COL.QUI."), tol=1)
    yield _linea_cmp("KR a terminado kg", d.get("KR"), _kg("GS.PROC."), tol=1)
    yield _linea_cmp("ITIN colorantes U$", d.get("ITIN"),
                     _f((tabla.get("COL.QUI.") or {}).get("us")), tol=1)
    yield line("  (difieren = movimientos tipeados en dBase después del último sync,")
    yield line("   o cargas PC (planilla/ajuste) que el dBase aún no tiene)")
    yield line()

    try:
        yield line("── [9b] TINTO del mes POR CÓDIGO (dBase | PC) — 1 a 1 ──")
        pc_tin = _pc_tinto_mes()
        db_tin = d.get("tinto_rows") or []

        def _agg_tin(rows):
            acc: dict = {}
            for r in rows:
                a = acc.setdefault(r["cod"], [0.0, 0.0, 0.0, 0])
                a[0] += _f(r["kg"]); a[1] += _f(r["kgn"])
                a[2] += _f(r["importe"]); a[3] += 1
            return acc
        A, B = _agg_tin(db_tin), _agg_tin(pc_tin)
        itin_db = sum(v[2] for v in A.values())
        itin_pc = sum(v[2] for v in B.values())
        yield line(f"  ITIN dBase={itin_db:,.2f} ({sum(v[3] for v in A.values())} filas)  "
                   f"PC={itin_pc:,.2f} ({sum(v[3] for v in B.values())} filas)  "
                   f"Δ={itin_pc - itin_db:+,.2f}")
        difs = []
        for c in sorted(set(A) | set(B)):
            a = A.get(c, [0.0, 0.0, 0.0, 0]); b = B.get(c, [0.0, 0.0, 0.0, 0])
            if any(abs(a[i] - b[i]) > 0.01 for i in (0, 1, 2)):
                difs.append((c, a, b))
        yield line(f"  códigos con diferencia: {len(difs)} (kg dBase|PC, kgn, U$, Δ U$)")
        for c, a, b in difs[:90]:
            yield line(f"    {c:5} kg {a[0]:>8,.0f}|{b[0]:>8,.0f}  kgn {a[1]:>8,.0f}|{b[1]:>8,.0f}  "
                       f"U$ {a[2]:>9,.2f}|{b[2]:>9,.2f}  ΔU$={b[2] - a[2]:>+10,.2f}  (n {a[3]}|{b[3]})")
        por_u: dict = {}
        for r in pc_tin:
            u = r.get("usuario") or "dbf-import"
            x = por_u.setdefault(u, [0.0, 0.0, 0])
            x[0] += _f(r["kg"]); x[1] += _f(r["importe"]); x[2] += 1
        yield line("  PC por usuario_crea: " + " ; ".join(
            f"{u or 'dbf-import'}: kg={v[0]:,.0f} U$={v[1]:,.2f} ({v[2]}f)"
            for u, v in sorted(por_u.items())))
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle tinto no disponible: {exc!r}]")
    yield line()

    try:
        yield line("── [9c] COMPRAS del mes 1 a 1 (fecha+tipo+prov+kg+importe) ──")
        pc_comp = _pc_compras_mes()
        db_comp = d.get("compras_mes_rows") or []

        def _tot_tipo(rows):
            t: dict = {}
            for r in rows:
                x = t.setdefault(r["tipo"], [0.0, 0.0, 0])
                x[0] += _f(r["kg"]); x[1] += _f(r["importe"]); x[2] += 1
            return t
        TA, TB = _tot_tipo(db_comp), _tot_tipo(pc_comp)
        for tp in sorted(set(TA) | set(TB)):
            a = TA.get(tp, [0.0, 0.0, 0]); b = TB.get(tp, [0.0, 0.0, 0])
            yield line(f"  tipo {tp or '?':2} kg {a[0]:>11,.2f}|{b[0]:>11,.2f} Δ{b[0] - a[0]:>+10,.2f}   "
                       f"U$ {a[1]:>11,.2f}|{b[1]:>11,.2f} Δ{b[1] - a[1]:>+10,.2f}  (n {a[2]}|{b[2]})")
        cdiff = _diff_multiset(
            db_comp, pc_comp,
            lambda r: (str(r.get("fecha") or ""), r.get("tipo"), r.get("prov"),
                       round(_f(r.get("kg")), 2), round(_f(r.get("importe")), 2)))
        yield line(f"  SOLO dBASE: {len(cdiff['solo_dbase'])}")
        for r in sorted(cdiff["solo_dbase"], key=lambda x: str(x.get("fecha")))[:MAX_LISTADO]:
            yield line(f"    {str(r.get('fecha') or ''):10} {r['tipo']:2} {r['prov']:4} "
                       f"kg={_f(r['kg']):>10,.2f} U$={_f(r['importe']):>10,.2f}")
        yield line(f"  SOLO PC: {len(cdiff['solo_pc'])}")
        for r in sorted(cdiff["solo_pc"], key=lambda x: str(x.get("fecha")))[:MAX_LISTADO]:
            yield line(f"    {str(r.get('fecha') or ''):10} {r['tipo']:2} {r['prov']:4} "
                       f"kg={_f(r['kg']):>10,.2f} U$={_f(r['importe']):>10,.2f} "
                       f"stat={r.get('stat') or ''} u={r.get('usuario') or ''}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle compras no disponible: {exc!r}]")
    yield line()

    stock = (res.get("stock") or {})
    yield line("── [10] STOCK POR ETAPA (PRG L313-345) ──")
    yield _linea_cmp("Hilado kg", d.get("HI"), _f((stock.get("hilado") or {}).get("kg")), tol=1)
    yield _linea_cmp("Tejido kg", d.get("TJ"), _f((stock.get("tejido") or {}).get("kg")), tol=1)
    yield _linea_cmp("Terminado kg", d.get("PF"), _f((stock.get("terminado") or {}).get("kg")), tol=1)
    yield cmp_acum("VSTO U$", d.get("VSTO"), _f(pc.get("vsto")))
    try:
        ip = _pc_iniciales_prev()
        yield line("  insumos de la fórmula (dBase | PC):")
        yield "  " + _linea_cmp("HI0 inic hilado kg", d.get("HI0"), ip["hilado"], tol=1)
        yield "  " + _linea_cmp("TJ0 inic tejido kg", d.get("TJ0"), ip["tejido"], tol=1)
        yield "  " + _linea_cmp("PF0 inic termin kg", d.get("PF0"), ip["terminado"], tol=1)
        yield "  " + _linea_cmp("UM0 tarifa prev", d.get("UM0"), ip["um"], tol=0.001)
        yield "  " + _linea_cmp("UMX tarifa live", d.get("UMX"),
                                _f((stock.get("hilado") or {}).get("ukg")), tol=0.001)
        kv_fis = iq.ventas_mes_corriente_kg_fisico()
        yield "  " + _linea_cmp("KV vendidos kg", d.get("KV"), _f(kv_fis), tol=1)
        yield "  " + _linea_cmp("KSTI servicios kg", d.get("KSTI"),
                                _f(iq.tinto_kg_servicios_mes()), tol=1)
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [insumos VSTO no disponibles: {exc!r}]")
    yield line()

    try:
        yield line("── [10b] KV kg vendidos del mes — factura x factura (fecha+kg) ──")
        pc_kv = _pc_facturas_kg_mes()
        db_kv = d.get("fact_kv_rows") or []
        kv_db = round(sum(_f(r["kg"]) for r in db_kv), 2)
        kv_pc = round(sum(_f(r["kg"]) for r in pc_kv), 2)
        yield line(f"  kg dBase={kv_db:,.2f} ({len(db_kv)}f)  PC={kv_pc:,.2f} ({len(pc_kv)}f)  "
                   f"Δ={kv_pc - kv_db:+,.2f}")
        kdiff = _diff_multiset(db_kv, pc_kv,
                               lambda r: (str(r.get("fecha") or ""), round(_f(r.get("kg")), 2)))
        yield line(f"  SOLO dBASE: {len(kdiff['solo_dbase'])}")
        for r in sorted(kdiff["solo_dbase"], key=lambda x: str(x.get("fecha")))[:MAX_LISTADO_FACT]:
            yield line(f"    {str(r.get('fecha') or ''):10} numf={r['numf']:<7} {r['cli']:4} "
                       f"stat={r['stat']:2} kg={_f(r['kg']):>10,.2f}")
        yield line(f"  SOLO PC: {len(kdiff['solo_pc'])}")
        for r in sorted(kdiff["solo_pc"], key=lambda x: str(x.get("fecha")))[:MAX_LISTADO_FACT]:
            yield line(f"    {str(r.get('fecha') or ''):10} numf={r['numf']:<7} {r['cli']:4} "
                       f"stat={r['stat']:2} kg={_f(r['kg']):>10,.2f}  u={r.get('usuario') or ''}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle KV no disponible: {exc!r}]")
    yield line()

    yield line("── [11] STOCK QUÍMICOS — VQX = VQ0+VQQ−ITIN (PRG L322) ──")
    yield cmp_acum("Stock Quí (VQX)", d.get("VQX"), _f(pc.get("vqx")))
    try:
        ip = ip if isinstance(ip, dict) else _pc_iniciales_prev()
    except NameError:
        ip = _pc_iniciales_prev()
    try:
        yield "  " + _linea_cmp("VQ0 inic químicos", d.get("VQ0"), ip.get("vq", 0.0))
        vqq_pc = sum(_f(r["importe"]) for r in (pc_comp if isinstance(pc_comp, list) else [])
                     if r.get("tipo") == "Q" and r.get("stat") not in ("X", "Y")
                     and r.get("usuario") != "asinfo-backfill")
        yield "  " + _linea_cmp("VQQ compras Q mes", d.get("VQQ"), vqq_pc)
        itin_pc2 = sum(_f(r["importe"]) for r in (pc_tin if isinstance(pc_tin, list) else []))
        yield "  " + _linea_cmp("ITIN tinto mes", d.get("ITIN"), itin_pc2)
        yield line("  (Δ VQX = ΔVQ0 + ΔVQQ − ΔITIN — el detalle por código está en [9b])")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [insumos VQX no disponibles: {exc!r}]")
    yield line()

    yield line("── [12] PATANT — HISTORIA mes anterior (PRG L281-284) ──")
    pat_db, pat_pc = d.get("patant"), _f(pc.get("patant"))
    yield _linea_cmp("Patrimonio cierre ant.", pat_db, pat_pc)
    if pat_db is not None and abs(pat_pc - pat_db) > 0.01:
        yield line("  ✗✗ PATANT DISTINTO — la utilidad de los dos programas NO es comparable")
        yield line("     hasta alinear el snapshot (ver /admin/regenerar-snapshot).")
    yield line()

    yield line("── [13] UTILIDAD — PATR − PATANT (PRG L380) ──")
    util_db = utilidad_dbase(d, totf_dbf) if totf_dbf is not None else None
    util_pc = _f(pc.get("utilidad"))
    yield _linea_cmp("UTILIDAD", util_db, util_pc)
    yield line()
    if util_db is not None:
        yield line("IDENTIDAD (Δutilidad explicada por componente, PC − dBase):")
        tot = 0.0
        if pat_db is not None:
            deltas.append(("PATANT (resta)", -(pat_pc - pat_db)))
        for label, dv in deltas:
            if abs(dv) > 0.005:
                yield line(f"  {label:24} {dv:>+14,.2f}")
            tot += dv
        residuo = (util_pc - util_db) - tot
        yield line(f"  {'Σ componentes':24} {tot:>+14,.2f}")
        yield line(f"  {'Δ utilidad':24} {util_pc - util_db:>+14,.2f}")
        ok = abs(residuo) <= 1.0
        yield line(f"  RESIDUO NO EXPLICADO: {residuo:,.2f}  {'✓ (todo explicado)' if ok else '✗✗ INVESTIGAR'}")
    yield line()
    yield line("Fin — ningún dato fue modificado.")


FORM = """
<!doctype html><meta charset=utf-8><title>dBase-compare</title>
<div style="max-width:680px;margin:2rem auto;font-family:system-ui">
<h2>Comparar PC vs dBase (sin sync, solo lectura)</h2>
<p>Subí el tarball con los DBF frescos (el mismo <code>dbf-fresh.tar.gz</code>
del sync — acá <b>no se importa nada</b>). El reporte calcula cada valor con
la regla del PRG y lo compara con el balance live de PC, cerrando con la
identidad de utilidad (residuo 0 = toda diferencia explicada).</p>
<form method=post action="/admin/dbase-compare/run" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz" required><br><br>
  <label>Detalle de movimientos de banco de los últimos
    <input type=number name=dias value=30 min=7 max=120 style="width:60px"> días</label><br><br>
  <button type=submit>Comparar</button>
</form></div>
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def form():
    return render_template_string(FORM)


@bp.route("/run", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def run():
    f = request.files.get("tarball")
    if not f or not f.filename:
        return Response("ERROR: falta el tarball.\n", mimetype="text/plain", status=400)
    if not f.filename.lower().endswith((".tar.gz", ".tgz")):
        return Response("ERROR: esperaba .tar.gz / .tgz.\n", mimetype="text/plain", status=400)
    try:
        dias = max(7, min(120, int(request.form.get("dias") or 30)))
    except (TypeError, ValueError):
        dias = 30

    TARBALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TARBALL_PATH.exists():
        TARBALL_PATH.unlink()
    f.save(TARBALL_PATH)
    if TARBALL_PATH.stat().st_size > MAX_TARBALL_BYTES:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response("ERROR: tarball muy grande.\n", mimetype="text/plain", status=400)

    return Response(stream_with_context(_run(dias)), mimetype="text/plain")


def _run(dias: int):
    import shutil

    def line(m=""):
        return m.rstrip("\n") + "\n"

    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            quiero = set(DBFS)
            for m in tar.getmembers():
                nombre = Path(m.name).name.upper()
                if m.isfile() and nombre in quiero:
                    m.name = nombre
                    tar.extract(m, EXTRACT_DIR)
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return
    try:
        yield from reporte(dias)
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] {exc!r}")
