"""Endpoint /admin/facturas-reconcile — compara scintela.factura con FACTURAS.DBF.

SOLO DRY-RUN (no escribe nada, nunca). Para facturas el "aplicar" ES el sync
normal (/admin/dbase-sync hace DELETE de filas no-backfill + INSERT del DBF).
Este reconciliador responde las 2 preguntas del estudio 2026-06-10:

  1. ¿PC tiene todo lo del dBase?  → bucket [A] SOLO dBASE (pendiente de sync).
  2. ¿Lo extra de PC es legítimo?  → buckets [B] backfill Asinfo (el sync las
     preserva), [C] creadas directo en PC (⚠ el próximo sync las BORRA si la
     fábrica no las tipeó en dBase — delete_where del import), [D] origen
     dbf-import que ya no está en el DBF (borradas/purgadas en dBase).

Además [E] DIFFS: misma factura con distinto saldo/abono/stat (cobranza
tipeada en dBase después del último sync — timing esperado).

Cierra con la IDENTIDAD TOTF:  PC = dBase − [A] + [B] + [C] + [D] + Δ[E]
(residuo 0,00 por construcción — es el self-check del bucketeo).

Pareo: clave (codigo_cli, numf) como multiset; dentro del grupo cancelan los
matches exactos (stat, importe, abono, saldo) y los sobrantes se aparean por
saldo ordenado → DIFF. Las numf=0 (entradas sin numerar del dBase) caen a
clave (codigo_cli, importe). Lector DBF = el _map_factura REAL del sync
(scripts/import_dbf.py, cargado por path) → paridad garantizada con lo que el
sync importaría. TMT 2026-06-10.
"""
from __future__ import annotations

import sys
import tarfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("facturas_reconcile", __name__, url_prefix="/admin/facturas-reconcile")

if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\facturas_reconcile.tar.gz")
    EXTRACT_DIR = Path(r"C:\facturas_reconcile")
else:
    TARBALL_PATH = Path("/tmp/facturas_reconcile.tar.gz")
    EXTRACT_DIR = Path("/tmp/facturas_reconcile")

MAX_TARBALL_BYTES = 40 * 1024 * 1024

BACKFILL_MARKER = "asinfo-backfill"
CARGA_MARKER = "asinfo-carga"
MAX_LISTADO = 40  # filas por sección en el reporte


def _norm_stat(s) -> str:
    return (s or "").strip().upper()


def _viva(stat) -> bool:
    """Regla TOTF (INFORMES.PRG L27): STAT $ "ZA" — Z, A y blank/NULL."""
    return _norm_stat(stat) in ("", "Z", "A")


def _saldo_za(r) -> float:
    """Aporte de la fila a TOTF: saldo si está viva, 0 si no."""
    return round(float(r.get("saldo") or 0), 2) if _viva(r.get("stat")) else 0.0


def _r2(x) -> float:
    return round(float(x or 0), 2)


def _firma(r) -> tuple:
    """Identidad económica exacta de la fila (para cancelar matches)."""
    return (_norm_stat(r.get("stat")), _r2(r.get("importe")),
            _r2(r.get("abono")), _r2(r.get("saldo")))


def _clave(r) -> tuple:
    numf = int(r.get("numf") or 0)
    cli = (r.get("codigo_cli") or "").strip().upper()
    if numf > 0:
        return ("N", cli, numf)
    # numf=0: entradas sin numerar del dBase → no hay clave; caemos a importe.
    return ("0", cli, _r2(r.get("importe")))


def reconciliar_facturas_plan(dbf_rows: list[dict], pc_rows: list[dict]) -> dict:
    """Calcula los buckets (puro, testeable, NO toca la base).

    dbf_rows: [{numf, codigo_cli, fecha, importe, abono, saldo, stat, tipo}]
    pc_rows:  idem + {id_factura, usuario_crea}
    Returns: {solo_dbase: [...], solo_pc_backfill: [...], solo_pc_directa: [...],
              solo_pc_dbf_huerfana: [...], diffs: [{pc, dbf, delta_za}],
              match: int}
    """
    dbf_g: dict = defaultdict(list)
    pc_g: dict = defaultdict(list)
    for r in dbf_rows:
        dbf_g[_clave(r)].append(r)
    for r in pc_rows:
        pc_g[_clave(r)].append(r)

    solo_dbase: list[dict] = []
    solo_pc: list[dict] = []
    diffs: list[dict] = []
    n_match = 0

    for k in sorted(set(list(dbf_g) + list(pc_g))):
        d_list = list(dbf_g.get(k, []))
        p_list = list(pc_g.get(k, []))

        # 1) cancelar matches exactos (multiset por firma económica)
        p_por_firma: dict = defaultdict(list)
        for p in p_list:
            p_por_firma[_firma(p)].append(p)
        d_rest = []
        for d in d_list:
            f = _firma(d)
            if p_por_firma.get(f):
                p_por_firma[f].pop()
                n_match += 1
            else:
                d_rest.append(d)
        p_rest = [p for lst in p_por_firma.values() for p in lst]

        # 2) sobrantes con misma clave → aparear por saldo ordenado = DIFF
        d_rest.sort(key=lambda r: _r2(r.get("saldo")))
        p_rest.sort(key=lambda r: _r2(r.get("saldo")))
        n = min(len(d_rest), len(p_rest))
        for i in range(n):
            diffs.append({
                "pc": p_rest[i], "dbf": d_rest[i],
                "delta_za": round(_saldo_za(p_rest[i]) - _saldo_za(d_rest[i]), 2),
            })
        solo_dbase.extend(d_rest[n:])
        solo_pc.extend(p_rest[n:])

    backfill, cargas, huerfanas, directas = [], [], [], []
    for r in solo_pc:
        uc = (r.get("usuario_crea") or "").strip()
        if uc == BACKFILL_MARKER:
            backfill.append(r)
        elif uc == CARGA_MARKER:
            cargas.append(r)
        elif uc in ("", "dbf-import"):
            huerfanas.append(r)
        else:
            directas.append(r)
    return {"solo_dbase": solo_dbase, "solo_pc_backfill": backfill,
            "solo_pc_carga": cargas, "solo_pc_directa": directas,
            "solo_pc_dbf_huerfana": huerfanas, "diffs": diffs, "match": n_match}


# ───────────────────────── lectura de fuentes ─────────────────────────

def _map_factura_real():
    """Carga el _map_factura REAL de scripts/import_dbf.py (paridad con sync)."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "scripts" / "import_dbf.py"
    spec = importlib.util.spec_from_file_location("_import_dbf_reconcile", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._map_factura


def _leer_dbf(dbf_path: Path) -> tuple[list[dict], int]:
    """Lee FACTURAS.DBF con el mapper del sync. Returns (filas, descartadas)."""
    import dbfread
    mapper = _map_factura_real()
    out, descartadas = [], 0
    for rec in dbfread.DBF(str(dbf_path), char_decode_errors="replace", load=False):
        row = mapper(rec)
        if row is None:  # stat legacy inmapeable — el sync también la filtra
            descartadas += 1
            continue
        out.append(row)
    return out, descartadas


def _leer_pc() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT id_factura, numf, codigo_cli, fecha, importe, abono, saldo,
               stat, tipo, usuario_crea
          FROM scintela.factura
        """
    ) or []
    return [dict(r) for r in rows]


# ───────────────────────── reporte ─────────────────────────

def _fmt(r, con_origen=False) -> str:
    base = (f"  {str(r.get('fecha') or ''):10} numf={int(r.get('numf') or 0):<7} "
            f"{(r.get('codigo_cli') or '').strip():5} stat={_norm_stat(r.get('stat')) or '·':2} "
            f"imp={_r2(r.get('importe')):>11,.2f} saldo={_r2(r.get('saldo')):>11,.2f}")
    if con_origen:
        base += f"  [{(r.get('usuario_crea') or 'NULL')[:18]}]"
    return base


def _seccion(titulo: str, filas: list[dict], con_origen=False):
    s_za = round(sum(_saldo_za(r) for r in filas), 2)
    s_total = round(sum(_r2(r.get("saldo")) for r in filas), 2)
    yield f"{titulo} — {len(filas)} filas · saldo ZA = {s_za:,.2f} (saldo total {s_total:,.2f})\n"
    for r in sorted(filas, key=lambda x: -abs(_r2(x.get("saldo"))))[:MAX_LISTADO]:
        yield _fmt(r, con_origen) + "\n"
    if len(filas) > MAX_LISTADO:
        yield f"  … y {len(filas) - MAX_LISTADO} más\n"
    yield "\n"


def reporte_desde_dbf(dbf_path: Path):
    """Genera el reporte dry-run (streaming). NO escribe nada."""
    def line(m=""):
        return m.rstrip("\n") + "\n"

    try:
        snapshot = (datetime.utcfromtimestamp(dbf_path.stat().st_mtime)
                    - timedelta(hours=5)).date()
        yield line(f"Snapshot DBF fechado {snapshot} (mtime)")
        hoy = (datetime.utcnow() - timedelta(hours=5)).date()
        if (hoy - snapshot).days > 7:
            yield line(f"  ⚠ snapshot de hace {(hoy - snapshot).days} días — ¿tarball viejo?")
    except Exception:  # noqa: BLE001
        pass

    dbf, descartadas = _leer_dbf(dbf_path)
    pc = _leer_pc()
    totf_dbf = round(sum(_saldo_za(r) for r in dbf), 2)
    totf_pc = round(sum(_saldo_za(r) for r in pc), 2)
    yield line(f"DBF: {len(dbf)} filas (+{descartadas} stat legacy descartadas, igual que el sync)"
               f" · TOTF(ZA) = {totf_dbf:,.2f}")
    totf_pc_app = round(sum(_saldo_za(r) for r in pc
                            if (r.get("usuario_crea") or "").strip() != BACKFILL_MARKER), 2)
    yield line(f"PC : {len(pc)} filas · TOTF(ZA) crudo = {totf_pc:,.2f} · "
               f"TOTF del programa (sin backfill) = {totf_pc_app:,.2f}")
    yield line(f"Delta crudo PC − dBase = {totf_pc - totf_dbf:,.2f}")
    yield line()

    plan = reconciliar_facturas_plan(dbf, pc)
    yield line(f"Matches exactos: {plan['match']}")
    yield line()

    yield from _seccion("[A] SOLO dBASE — el próximo /admin/dbase-sync las trae",
                        plan["solo_dbase"])
    yield from _seccion("[B1] SOLO PC — backfill Asinfo automático (NO cuenta en "
                        "cartera; el sync la preserva)",
                        plan["solo_pc_backfill"])
    yield from _seccion("[B2] SOLO PC — asinfo-carga (botón Cargar: CUENTA en cartera; "
                        "si el DBF la trae, dBase gana y se absorbe)",
                        plan["solo_pc_carga"])
    yield from _seccion("[C] SOLO PC — creadas directo en PC "
                        "(⚠ el próximo sync las BORRA si no están tipeadas en dBase)",
                        plan["solo_pc_directa"], con_origen=True)
    yield from _seccion("[D] SOLO PC — origen dbf-import que YA NO está en el DBF "
                        "(borradas/modificadas con clave en dBase; el sync las saca)",
                        plan["solo_pc_dbf_huerfana"])

    d = plan["diffs"]
    delta_diffs = round(sum(x["delta_za"] for x in d), 2)
    yield line(f"[E] DIFFS (misma factura, cambió saldo/abono/stat — cobranza post-sync) — "
               f"{len(d)} filas · Δ TOTF = {delta_diffs:,.2f}")
    for x in sorted(d, key=lambda v: -abs(v["delta_za"]))[:MAX_LISTADO]:
        p, b = x["pc"], x["dbf"]
        yield line(f"  numf={int(p.get('numf') or 0):<7} {(p.get('codigo_cli') or '').strip():5} "
                   f"PC: stat={_norm_stat(p.get('stat')) or '·'} saldo={_r2(p.get('saldo')):>11,.2f}  "
                   f"dBase: stat={_norm_stat(b.get('stat')) or '·'} saldo={_r2(b.get('saldo')):>11,.2f}  "
                   f"Δza={x['delta_za']:>+10,.2f}")
    if len(d) > MAX_LISTADO:
        yield line(f"  … y {len(d) - MAX_LISTADO} más")
    yield line()

    # Identidad (self-check del bucketeo — residuo 0,00 por construcción)
    s = {k: round(sum(_saldo_za(r) for r in plan[k]), 2)
         for k in ("solo_dbase", "solo_pc_backfill", "solo_pc_carga",
                   "solo_pc_directa", "solo_pc_dbf_huerfana")}
    residuo = round((totf_pc - totf_dbf)
                    - (-s["solo_dbase"] + s["solo_pc_backfill"] + s["solo_pc_carga"]
                       + s["solo_pc_directa"] + s["solo_pc_dbf_huerfana"] + delta_diffs), 2)
    yield line("IDENTIDAD TOTF:")
    yield line(f"  PC ({totf_pc:,.2f}) = dBase ({totf_dbf:,.2f})"
               f" − A({s['solo_dbase']:,.2f}) + B1({s['solo_pc_backfill']:,.2f})"
               f" + B2({s['solo_pc_carga']:,.2f})"
               f" + C({s['solo_pc_directa']:,.2f}) + D({s['solo_pc_dbf_huerfana']:,.2f})"
               f" + ΔE({delta_diffs:,.2f})")
    yield line(f"  residuo = {residuo:,.2f}  {'✓' if abs(residuo) < 0.01 else '✗ REVISAR'}")
    yield line()
    yield line("DRY-RUN: no se tocó nada (este endpoint nunca escribe). "
               "Para alinear: correr /admin/dbase-sync (A/D/E) y revisar [C] a mano.")


FORM = """
<!doctype html><meta charset=utf-8><title>Reconciliar FACTURAS (dry-run)</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>Facturas: PC vs dBase (dry-run)</h2>
<p>Subí el tarball con FACTURAS.DBF (el mismo de /admin/dbase-sync).
<b>Solo lectura</b>: compara y reporta, no escribe nunca. Responde:
¿PC tiene todo lo del dBase? ¿lo extra de PC es legítimo?</p>
<form method=post action="/admin/facturas-reconcile/run" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz" required><br><br>
  <button type=submit>Correr dry-run</button>
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

    yield line("=== Reconciliar FACTURAS — DRY-RUN (solo lectura) ===")
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
        yield from reporte_desde_dbf(miembro)
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] {exc!r}")


# ─────────────────────── diag por cliente (sin tarball) ───────────────────────
#
# Reporte que solo lee scintela.factura y agrupa por (codigo_cli, usuario_crea).
# Sirve para responder "por qué el cliente X aparece con saldo en /cartera si
# el dBase ya no tiene facturas suyas" sin tener que correr el reconcile pesado
# ni subir el tarball. La regla de saldo/ZA es idéntica al reconciliador.


@bp.route("/pc-por-cliente", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def pc_por_cliente():
    """Diag: top clientes con saldo pendiente en PC clasificado por usuario_crea.

    Sin filtro → top 30 por saldo pendiente vivo. Con `?codigo_cli=NJL` →
    detalle fila-por-fila del cliente pedido. Puramente lectura.
    """
    import db

    filtro = (request.args.get("codigo_cli") or "").strip().upper()

    def gen():
        yield "=== Facturas por cliente (PC, solo lectura) ===\n"
        if filtro:
            yield f"Filtro: codigo_cli = {filtro!r}\n\n"
            rows = db.fetch_all(
                """
                SELECT id_factura, numf, numf_completo, fecha,
                       importe, abono, saldo, stat,
                       COALESCE(usuario_crea, '') AS usuario_crea
                  FROM scintela.factura
                 WHERE codigo_cli = %s
                 ORDER BY fecha, numf
                """,
                (filtro,),
            ) or []
            if not rows:
                yield "(sin facturas en PC para este cliente)\n"
                return
            yield f"{len(rows)} facturas en PC.\n\n"
            # Desglose por usuario_crea
            por_origen = defaultdict(lambda: {"n": 0, "saldo_za": 0.0, "saldo_tot": 0.0})
            for r in rows:
                o = por_origen[r["usuario_crea"] or "(vacío)"]
                o["n"] += 1
                o["saldo_tot"] += _r2(r.get("saldo"))
                o["saldo_za"] += _saldo_za(r)
            yield "Por origen (usuario_crea):\n"
            for k, v in sorted(por_origen.items(), key=lambda kv: -abs(kv[1]["saldo_za"])):
                yield (f"  {k:20} · {v['n']:4} filas · "
                       f"saldo ZA = {v['saldo_za']:>13,.2f} · "
                       f"saldo total = {v['saldo_tot']:>13,.2f}\n")
            yield "\nDetalle:\n"
            for r in rows:
                yield (f"  {str(r.get('fecha') or ''):10} "
                       f"numf={int(r.get('numf') or 0):<7} "
                       f"stat={_norm_stat(r.get('stat')) or '·':2} "
                       f"imp={_r2(r.get('importe')):>11,.2f} "
                       f"saldo={_r2(r.get('saldo')):>11,.2f}  "
                       f"[{r['usuario_crea'] or '(vacío)'}]\n")
            # Bonus — chequesxfact del cliente y con qué factura linkean
            cxf = db.fetch_all(
                """
                SELECT cf.id_chequexfact, cf.id_cheque, cf.id_fact,
                       cf.importe, cf.fechaing,
                       c.no_cheque, COALESCE(c.no_banco, 0) AS no_banco,
                       f.numf, f.stat AS fact_stat,
                       COALESCE(f.usuario_crea, '(fila borrada / null)') AS fact_origen
                  FROM scintela.chequesxfact cf
                  LEFT JOIN scintela.cheque  c ON c.id_cheque  = cf.id_cheque
                  LEFT JOIN scintela.factura f ON f.id_factura = cf.id_fact
                 WHERE cf.codigo_cli = %s
                 ORDER BY cf.fechaing, cf.id_chequexfact
                """,
                (filtro,),
            ) or []
            yield f"\nChequesxfact del cliente: {len(cxf)}\n"
            for r in cxf:
                yield (f"  cxf#{r['id_chequexfact']} chq={r['no_cheque']} nb={r['no_banco']} "
                       f"imp={_r2(r.get('importe')):>10,.2f} "
                       f"→ id_fact={r['id_fact']} numf={r.get('numf') or '-'} "
                       f"stat={r.get('fact_stat') or '-'} [{r['fact_origen']}]\n")
            return

        # Sin filtro — top 30 por saldo pendiente ZA, con desglose backfill vs no-backfill.
        yield "Sin filtro → top clientes con SALDO PENDIENTE que viene SOLO de backfill Asinfo.\n"
        yield "(saldo_backfill_za = suma de saldos con stat en ZA y usuario_crea='asinfo-backfill')\n\n"
        rows = db.fetch_all(
            """
            SELECT codigo_cli,
                   COUNT(*) FILTER (
                       WHERE COALESCE(usuario_crea,'') = 'asinfo-backfill'
                         AND COALESCE(saldo,0) <> 0
                         AND (stat IS NULL OR stat IN ('Z','A','',' '))
                   ) AS n_backfill_vivas,
                   COALESCE(SUM(CASE
                       WHEN COALESCE(usuario_crea,'') = 'asinfo-backfill'
                        AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       THEN saldo ELSE 0 END), 0) AS saldo_backfill_za,
                   COALESCE(SUM(CASE
                       WHEN COALESCE(usuario_crea,'') <> 'asinfo-backfill'
                        AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       THEN saldo ELSE 0 END), 0) AS saldo_no_backfill_za
              FROM scintela.factura
             GROUP BY codigo_cli
            HAVING COALESCE(SUM(CASE
                       WHEN COALESCE(usuario_crea,'') = 'asinfo-backfill'
                        AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       THEN saldo ELSE 0 END), 0) <> 0
             ORDER BY ABS(SUM(CASE
                       WHEN COALESCE(usuario_crea,'') = 'asinfo-backfill'
                        AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       THEN saldo ELSE 0 END)) DESC
             LIMIT 30
            """
        ) or []
        if not rows:
            yield "(no hay clientes con saldo backfill vivo — ¡nada que limpiar!)\n"
            return
        # Total agregado
        totales = db.fetch_one(
            """
            SELECT COUNT(*) FILTER (
                       WHERE COALESCE(usuario_crea,'') = 'asinfo-backfill'
                         AND COALESCE(saldo,0) <> 0
                         AND (stat IS NULL OR stat IN ('Z','A','',' '))
                   ) AS n_backfill_vivas_total,
                   COUNT(DISTINCT codigo_cli) FILTER (
                       WHERE COALESCE(usuario_crea,'') = 'asinfo-backfill'
                         AND COALESCE(saldo,0) <> 0
                         AND (stat IS NULL OR stat IN ('Z','A','',' '))
                   ) AS n_clientes_afectados,
                   COALESCE(SUM(CASE
                       WHEN COALESCE(usuario_crea,'') = 'asinfo-backfill'
                        AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       THEN saldo ELSE 0 END), 0) AS saldo_backfill_total
              FROM scintela.factura
            """
        ) or {}
        yield (f"TOTALES: {totales.get('n_backfill_vivas_total', 0)} facturas backfill 'vivas' "
               f"en {totales.get('n_clientes_afectados', 0)} clientes · "
               f"saldo ZA total = {_r2(totales.get('saldo_backfill_total', 0)):,.2f}\n\n")
        yield f"{'CLI':5} {'N_BFvivas':>9} {'saldo_BF_ZA':>13} {'saldo_no_BF_ZA':>15}\n"
        yield "-" * 50 + "\n"
        for r in rows:
            yield (f"{(r.get('codigo_cli') or '')[:5]:5} "
                   f"{r.get('n_backfill_vivas', 0):>9} "
                   f"{_r2(r.get('saldo_backfill_za', 0)):>13,.2f} "
                   f"{_r2(r.get('saldo_no_backfill_za', 0)):>15,.2f}\n")
        yield "\nPara ver el detalle de un cliente: ?codigo_cli=NJL\n"

    return Response(stream_with_context(gen()), mimetype="text/plain")
