"""Endpoint /admin/dbase-compare — comparador sistemático PC vs dBase SIN sync.

Pedido dueña 2026-06-10: "construí todos los checks que tenemos que hacer
para ver cómo diferimos del dBase, así es sistemático. No quiero hacer sync.
Quiero poder comparar exacto ya que se están usando los dos programas."

SOLO LECTURA — no escribe nada, nunca. Subís el tarball con los DBF frescos
(el mismo dbf-fresh.tar.gz del sync, pero acá NO se importa nada) y el
reporte calcula cada valor del lado dBase con la regla EXACTA del PRG
(INFORMES.PRG, citada por línea — todas validadas contra pantalla/HISTORIA
el 2026-06-10) y lo compara con el balance live de PC (informe_balance()).

Secciones:
  [1] Caja            CAJA.DBF último saldo            vs salcaj
  [2] Bancos          PICHINCH/INTER último saldo      vs saldo PC
      + DETALLE por movimiento (fecha+|importe| multiset): qué movs tiene
      el dBase que PC no (p.ej. los pagos AI del 09/06) y viceversa.
  [3] Cheques  TOTC   STAT $ 'Z123PD' (L24)            vs totc
  [4] Facturas TOTF   STAT $ 'ZA' (L27) + buckets del facturas-reconcile
  [5] Anticipos       DOLARES ST=' ' (L58)             vs antic
  [6] Pasivos  TOTP   POSDAT BANC#9 (L55)              vs totp
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

    pich = _leer("PICHINCH.DBF")
    inter = _leer("INTER.DBF")
    d["salbanc1"] = _f(pich[-1].get("SALDO")) if pich else None
    d["salbanc2"] = _f(inter[-1].get("SALDO")) if inter else None
    d["pich_movs"] = [{"fecha": r.get("FECHA"), "doc": (r.get("DOC") or "").strip(),
                       "importe": round(_f(r.get("IMPORTE")), 2),
                       "concepto": (r.get("CONCEPTO") or "").strip()} for r in pich]
    d["pich_mtime"] = None
    p = EXTRACT_DIR / "PICHINCH.DBF"
    if p.exists():
        d["pich_mtime"] = datetime.utcfromtimestamp(p.stat().st_mtime) - timedelta(hours=5)

    d["totc"] = sum(_f(r.get("IMPORTE")) for r in _leer("CHEQUES.DBF")
                    if (r.get("STAT") or "").strip() in ("Z", "1", "2", "3", "P", "D"))

    d["antic"] = sum(_f(r.get("IMPORTE")) for r in _leer("DOLARES.DBF")
                     if (r.get("ST") or " ").strip() == "")

    d["totp"] = sum(_f(r.get("IMPORTE")) for r in _leer("POSDAT.DBF")
                    if int(r.get("BANC") or 0) != 9)

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
    KV = sum(_f(r.get("KG")) for r in _leer("FACTURAS.DBF")
             if r.get("FECHA") and r["FECHA"].year == anio and r["FECHA"].month == mes
             and (r.get("STAT") or "").strip() in ("Z", "A", "T", "P")
             and (r.get("TIPO") or "").strip() != "S")
    d.update(KM=KM, VM=VM, KK=KK, KTINT=KTINT, KR=KR, KSTI=KSTI, ITIN=ITIN,
             KV=KV, VQQ=VQQ, KT_c=KT_c)

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
                 VQX=VQ0 + VQQ - ITIN, HI0=HI0, TJ0=TJ0, PF0=PF0)

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
               COALESCE(t.concepto, '') AS concepto
          FROM scintela.transacciones_bancarias t
          JOIN scintela.banco b ON b.no_banco = t.no_banco
         WHERE UPPER(b.nombre) LIKE 'PICHINCH%%'
           AND t.fecha >= %s
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


# ───────────────── reporte ─────────────────

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

    def cmp_acum(label, db_v, pc_v, activo=True):
        nonlocal deltas
        if db_v is not None:
            deltas.append((label, (pc_v - db_v) * (1 if activo else -1)))
        return _linea_cmp(label, db_v, pc_v)

    yield line("── [1] CAJA (CAJA.DBF último saldo) ──")
    yield cmp_acum("Caja", d.get("salcaj"), _f(pc.get("salcaj")))
    yield line()

    yield line("── [2] BANCOS (último saldo del DBF vs PC) ──")
    pc_b1 = next((_f(b.get("saldo")) for b in pc.get("bancos_todos") or []
                  if "PICHINCH" in str(b.get("nombre") or "").upper()), _f(pc.get("salbanc")))
    yield cmp_acum("Pichincha", d.get("salbanc1"), pc_b1)
    if d.get("salbanc2") is not None:
        pc_b2 = next((_f(b.get("saldo")) for b in pc.get("bancos_todos") or []
                      if "INTER" in str(b.get("nombre") or "").upper()), 0.0)
        yield cmp_acum("Internacional", d.get("salbanc2"), pc_b2)
    desde = _hoy_ec() - timedelta(days=dias_banco)
    try:
        movdiff = diff_movs_banco(d.get("pich_movs") or [], _pc_movs_pichincha(desde), desde)
        yield line(f"  movimientos desde {desde} — SOLO dBASE (PC no los tiene): "
                   f"{len(movdiff['solo_dbase'])}")
        for m in sorted(movdiff["solo_dbase"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {m['doc']:3} {m['importe']:>12,.2f}  {m['concepto'][:34]}")
        yield line(f"  movimientos desde {desde} — SOLO PC (dBase no los tiene): "
                   f"{len(movdiff['solo_pc'])}")
        for m in sorted(movdiff["solo_pc"], key=lambda x: str(x.get("fecha")))[-MAX_LISTADO:]:
            yield line(f"    {m['fecha']} {str(m['doc'])[:3]:3} {_f(m['importe']):>12,.2f}  "
                       f"{str(m['concepto'])[:34]}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"  [detalle movimientos no disponible: {exc!r}]")
    yield line()

    yield line("── [3] CHEQUES — TOTC = STAT $ 'Z123PD' (PRG L24) ──")
    yield cmp_acum("Cheques (TOTC)", d.get("totc"), _f(pc.get("totc")))
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
    yield line()

    yield line("── [5] ANTICIPOS — DOLARES ST=' ' (PRG L58) ──")
    yield cmp_acum("Anticipos", d.get("antic"), _f(pc.get("antic")))
    yield line()

    yield line("── [6] PASIVOS — TOTP = POSDAT BANC#9 (PRG L55) ──")
    yield cmp_acum("Pasivos (TOTP)", d.get("totp"), _f(pc.get("totp")), activo=False)
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

    stock = (res.get("stock") or {})
    yield line("── [10] STOCK POR ETAPA (PRG L313-345) ──")
    yield _linea_cmp("Hilado kg", d.get("HI"), _f((stock.get("hilado") or {}).get("kg")), tol=1)
    yield _linea_cmp("Tejido kg", d.get("TJ"), _f((stock.get("tejido") or {}).get("kg")), tol=1)
    yield _linea_cmp("Terminado kg", d.get("PF"), _f((stock.get("terminado") or {}).get("kg")), tol=1)
    yield cmp_acum("VSTO U$", d.get("VSTO"), _f(pc.get("vsto")))
    yield line()

    yield line("── [11] STOCK QUÍMICOS — VQX = VQ0+VQQ−ITIN (PRG L322) ──")
    yield cmp_acum("Stock Quí (VQX)", d.get("VQX"), _f(pc.get("vqx")))
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
