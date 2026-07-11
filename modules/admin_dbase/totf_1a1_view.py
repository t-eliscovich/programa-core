"""Endpoint /admin/totf-1a1 — pareo COMPLETO factura por factura PC vs dBase.

TMT 2026-06-11 — cierre del residual Δ TOTF (+4.896,63 post mig 0090).
SOLO LECTURA: no escribe nada, nunca. Es el mismo pareo por N° SRI del
dbase-compare [4] pero SIN truncar y con el detalle que hace falta para
decidir 1 a 1 (regla dueña: solo lo que está en el dBase, nunca bulk):

  - vivas dBASE sin par vivo en PC  → ¿PC la tiene como backfill/otro stat?
    (si está como backfill, el fix 1 a 1 es flipear ESA fila puntual)
  - vivas PC (que cuentan) sin par vivo en dBase → ¿el DBF la tiene con
    otro stat (T/X = cobrada/anulada → timing de cobranza) o NO existe
    (posible doble por alias / directa-PC)?
  - pares apareados por SRI con SALDO distinto (cobranza post-tarball)
  - NUMF=0 (sin N° SRI): pareo por (cliente, importe)
  - identidad: Δ TOTF = solo_pc − solo_dbase + Δmatched + Δsin_sri, residuo 0
"""
from __future__ import annotations

import sys
import tarfile
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("totf_1a1", __name__, url_prefix="/admin/totf-1a1")

if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\totf_1a1.tar.gz")
    EXTRACT_DIR = Path(r"C:\totf_1a1")
else:
    TARBALL_PATH = Path("/tmp/totf_1a1.tar.gz")
    EXTRACT_DIR = Path("/tmp/totf_1a1")

MAX_TARBALL_BYTES = 40 * 1024 * 1024
BACKFILL = "asinfo-backfill"


def _r2(x) -> float:
    return round(float(x or 0), 2)


def _viva(stat) -> bool:
    return (stat or "").strip().upper() in ("", "Z", "A")


def _sri(r) -> int:
    """N° SRI: últimos dígitos de numf_completo; fallback numf."""
    import re
    m = re.search(r"(\d+)$", (r.get("numf_completo") or "").strip())
    if m:
        return int(m.group(1))
    return int(r.get("numf") or 0)


def _pc_all() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, codigo_cli, fecha, importe,
               abono, saldo, COALESCE(stat,'') AS stat, tipo,
               COALESCE(usuario_crea,'') AS usuario_crea, fecha_crea
          FROM scintela.factura
        """
    ) or []
    return [dict(r) for r in rows]


def _fmt_db(r) -> str:
    return (f"{str(r.get('fecha') or ''):10} numf={int(r.get('numf') or 0):<7} "
            f"{(r.get('codigo_cli') or '').strip():5} "
            f"stat={(r.get('stat') or '').strip() or '·':2} "
            f"imp={_r2(r.get('importe')):>11,.2f} saldo={_r2(r.get('saldo')):>11,.2f}")


def _fmt_pc(r) -> str:
    fc = str(r.get("fecha_crea") or "")[:16]
    return (f"{str(r.get('fecha') or ''):10} sri={_sri(r):<7} "
            f"{(r.get('codigo_cli') or '').strip():5} "
            f"stat={(r.get('stat') or '').strip() or '·':2} "
            f"imp={_r2(r.get('importe')):>11,.2f} saldo={_r2(r.get('saldo')):>11,.2f} "
            f"id={r.get('id_factura')} [{(r.get('usuario_crea') or 'NULL')[:16]}] crea={fc}")


def reporte(dbf_rows: list[dict], pc_rows: list[dict]):
    def line(m=""):
        return m.rstrip("\n") + "\n"

    db_vivas = [r for r in dbf_rows if _viva(r.get("stat"))]
    pc_cuentan = [r for r in pc_rows
                  if (r.get("usuario_crea") or "").strip() != BACKFILL]
    pc_vivas = [r for r in pc_cuentan if _viva(r.get("stat"))]

    totf_db = round(sum(_r2(r["saldo"]) for r in db_vivas), 2)
    totf_pc = round(sum(_r2(r["saldo"]) for r in pc_vivas), 2)
    yield line(f"TOTF dBase = {totf_db:,.2f} · TOTF PC (sin backfill) = {totf_pc:,.2f} "
               f"· Δ = {round(totf_pc - totf_db, 2):+,.2f}")
    yield line()

    # índices auxiliares para los cross-checks
    pc_por_sri: dict = defaultdict(list)   # TODAS las filas PC (incl. backfill, T, X)
    for r in pc_rows:
        k = _sri(r)
        if k > 0:
            pc_por_sri[k].append(r)
    db_por_sri_all: dict = defaultdict(list)  # TODAS las filas del DBF
    for r in dbf_rows:
        k = int(r.get("numf") or 0)
        if k > 0:
            db_por_sri_all[k].append(r)

    # ── 1) pareo por N° SRI (vivas) ──
    db_g, pc_g = defaultdict(list), defaultdict(list)
    for r in db_vivas:
        if int(r.get("numf") or 0) > 0:
            db_g[int(r["numf"])].append(r)
    for r in pc_vivas:
        if _sri(r) > 0:
            pc_g[_sri(r)].append(r)
    db_sin_sri = [r for r in db_vivas if int(r.get("numf") or 0) <= 0]
    pc_sin_sri = [r for r in pc_vivas if _sri(r) <= 0]

    solo_db, solo_pc, matched_diff = [], [], []
    d_matched = 0.0
    for k in sorted(set(db_g) | set(pc_g)):
        a = sorted(db_g.get(k, []), key=lambda r: _r2(r["saldo"]))
        b = sorted(pc_g.get(k, []), key=lambda r: _r2(r["saldo"]))
        n = min(len(a), len(b))
        for i in range(n):
            d = round(_r2(b[i]["saldo"]) - _r2(a[i]["saldo"]), 2)
            d_matched = round(d_matched + d, 2)
            if abs(d) >= 0.01:
                matched_diff.append((a[i], b[i], d))
        solo_db.extend(a[n:])
        solo_pc.extend(b[n:])

    s_solo_db = round(sum(_r2(r["saldo"]) for r in solo_db), 2)
    s_solo_pc = round(sum(_r2(r["saldo"]) for r in solo_pc), 2)

    yield line(f"== [1] vivas dBASE sin par vivo en PC: {len(solo_db)} "
               f"(saldo {s_solo_db:,.2f}) ==")
    yield line("   (al lado: qué tiene PC con ese mismo N° SRI, si tiene)")
    for r in sorted(solo_db, key=lambda x: int(x.get("numf") or 0)):
        yield line(f"  {_fmt_db(r)}")
        otros = pc_por_sri.get(int(r["numf"]) or -1, [])
        if not otros:
            yield line("      PC: NO EXISTE ese N° SRI en ninguna fila")
        for o in otros:
            yield line(f"      PC: {_fmt_pc(o)}")
    yield line()

    yield line(f"== [2] vivas PC (cuentan) sin par vivo en dBase: {len(solo_pc)} "
               f"(saldo {s_solo_pc:,.2f}) ==")
    yield line("   (al lado: qué tiene el DBF con ese N° SRI, si tiene)")
    for r in sorted(solo_pc, key=_sri):
        yield line(f"  {_fmt_pc(r)}")
        otros = db_por_sri_all.get(_sri(r), [])
        if not otros:
            yield line("      DBF: NO EXISTE ese N° SRI en ninguna fila")
        for o in otros:
            yield line(f"      DBF: {_fmt_db(o)}")
    yield line()

    yield line(f"== [3] apareadas por SRI con SALDO distinto: {len(matched_diff)} "
               f"(Δ acumulado {d_matched:+,.2f}) ==")
    for a, b, d in sorted(matched_diff, key=lambda x: -abs(x[2])):
        yield line(f"  Δ={d:>+10,.2f}  dBase: {_fmt_db(a)}")
        yield line(f"               PC   : {_fmt_pc(b)}")
    yield line()

    # ── 2) sin N° SRI: pareo por (cliente, importe) ──
    g_a, g_b = defaultdict(list), defaultdict(list)
    for r in db_sin_sri:
        g_a[((r.get("codigo_cli") or "").strip().upper(), _r2(r["importe"]))].append(r)
    for r in pc_sin_sri:
        g_b[((r.get("codigo_cli") or "").strip().upper(), _r2(r["importe"]))].append(r)
    s0_db, s0_pc = [], []
    d0_matched = 0.0
    matched0_diff = []
    for k in set(g_a) | set(g_b):
        a = sorted(g_a.get(k, []), key=lambda r: _r2(r["saldo"]))
        b = sorted(g_b.get(k, []), key=lambda r: _r2(r["saldo"]))
        n = min(len(a), len(b))
        for i in range(n):
            d = round(_r2(b[i]["saldo"]) - _r2(a[i]["saldo"]), 2)
            d0_matched = round(d0_matched + d, 2)
            if abs(d) >= 0.01:
                matched0_diff.append((a[i], b[i], d))
        s0_db.extend(a[n:])
        s0_pc.extend(b[n:])
    ss0_db = round(sum(_r2(r["saldo"]) for r in s0_db), 2)
    ss0_pc = round(sum(_r2(r["saldo"]) for r in s0_pc), 2)
    yield line(f"== [4] SIN N° SRI (NUMF=0) — pareo por (cliente, importe): "
               f"dBase {len(db_sin_sri)} vs PC {len(pc_sin_sri)} ==")
    yield line(f"  solo dBASE: {len(s0_db)} (saldo {ss0_db:,.2f})")
    for r in sorted(s0_db, key=lambda x: str(x.get("fecha"))):
        yield line(f"    {_fmt_db(r)}")
    yield line(f"  solo PC: {len(s0_pc)} (saldo {ss0_pc:,.2f})")
    for r in sorted(s0_pc, key=lambda x: str(x.get("fecha"))):
        yield line(f"    {_fmt_pc(r)}")
    yield line(f"  apareadas con saldo distinto: {len(matched0_diff)} "
               f"(Δ acumulado {d0_matched:+,.2f})")
    for a, b, d in sorted(matched0_diff, key=lambda x: -abs(x[2])):
        yield line(f"    Δ={d:>+10,.2f}  dBase: {_fmt_db(a)}")
        yield line(f"                 PC   : {_fmt_pc(b)}")
    yield line()

    delta = round(totf_pc - totf_db, 2)
    expl = round(s_solo_pc - s_solo_db + d_matched + ss0_pc - ss0_db + d0_matched, 2)
    yield line("IDENTIDAD:")
    yield line(f"  Δ TOTF ({delta:+,.2f}) = soloPC({s_solo_pc:,.2f}) − soloDB({s_solo_db:,.2f})"
               f" + Δmatched({d_matched:+,.2f}) + sinSRI soloPC({ss0_pc:,.2f})"
               f" − sinSRI soloDB({ss0_db:,.2f}) + Δmatched0({d0_matched:+,.2f})")
    yield line(f"  residuo = {round(delta - expl, 2):,.2f}  "
               f"{'✓' if abs(delta - expl) < 0.01 else '✗ REVISAR'}")
    yield line()
    yield line("SOLO LECTURA: no se modificó nada.")


FORM = """
<!doctype html><meta charset=utf-8><title>TOTF 1 a 1 (solo lectura)</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>TOTF factura por factura — PC vs dBase</h2>
<p>Subí el tarball con FACTURAS.DBF. <b>Solo lectura</b>: pareo completo por
N° SRI sin truncar, con cross-check de backfill y stats del otro lado.</p>
<form method=post action="/admin/totf-1a1/run" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz,application/gzip,application/x-gzip,application/x-tar" required><br><br>
  <button type=submit>Correr (dry-run)</button>
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
    TARBALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TARBALL_PATH.exists():
        TARBALL_PATH.unlink()
    f.save(TARBALL_PATH)
    if TARBALL_PATH.stat().st_size > MAX_TARBALL_BYTES:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response("ERROR: tarball muy grande.\n", mimetype="text/plain", status=400)
    return Response(stream_with_context(_run()), mimetype="text/plain")


def _run():
    import shutil

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line("=== TOTF 1 a 1 — DRY-RUN (solo lectura) ===")
    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        miembro = None
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and Path(m.name).name.upper() == "FACTURAS.DBF":
                    m.name = "FACTURAS.DBF"
                    tar.extract(m, EXTRACT_DIR)
                    miembro = EXTRACT_DIR / "FACTURAS.DBF"
                    break
        if not miembro or not miembro.exists():
            yield line("[ERROR] el tarball no contiene FACTURAS.DBF")
            return
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return

    try:
        from modules.admin_dbase.facturas_reconcile_view import _map_factura_real
        import dbfread
        mapper = _map_factura_real()
        dbf_rows = []
        for rec in dbfread.DBF(str(miembro), char_decode_errors="replace", load=False):
            row = mapper(rec)
            if row is not None:
                dbf_rows.append(row)
        yield from reporte(dbf_rows, _pc_all())
    except Exception as exc:  # noqa: BLE001
        import traceback
        yield line(f"[ERROR] {exc!r}")
        yield line(traceback.format_exc())
