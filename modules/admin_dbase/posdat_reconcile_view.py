"""Endpoint /admin/posdat-reconcile — reconcilia scintela.posdat con POSDAT.DBF.

POSDAT está en NEVER_EXTRACT del sync normal porque tiene estado propio de PC
(id_posdat referenciado por mov_doble, columnas YY de acumulación, anulada).
Un TRUNCATE+INSERT lo rompería. Este reconciliador es QUIRÚRGICO:

- Pareo por proveedor (DBF.num=0 → no hay clave; pareamos por importe ordenado).
- Las que matchean → UPDATE in-place (CONSERVA id_posdat → no rompe mov_doble).
  · YY: además fija baseline_date = HOY (la acumulación sigue desde el valor dBase).
- Las que sobran en PC → DELETE (si tienen link de compra, se SALTAN y se reportan).
- Las que faltan (están en dBase, no en PC) → INSERT.

Resultado: scintela.posdat (banc=0) = POSDAT.DBF (banc=0) → Pasivos = dBase.

Flujo: subís el tarball (mismo que /admin/dbase-sync), corre en DRY-RUN por
defecto (no toca nada, muestra el plan). Con el botón "Aplicar" (?apply=1) lo
ejecuta en una transacción. TMT 2026-06-05.
"""
from __future__ import annotations

import sys
import tarfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("posdat_reconcile", __name__, url_prefix="/admin/posdat-reconcile")

if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\posdat_reconcile.tar.gz")
    EXTRACT_DIR = Path(r"C:\posdat_reconcile")
else:
    TARBALL_PATH = Path("/tmp/posdat_reconcile.tar.gz")
    EXTRACT_DIR = Path("/tmp/posdat_reconcile")

MAX_TARBALL_BYTES = 10 * 1024 * 1024


def _hoy_ec() -> date:
    return (datetime.utcnow() - timedelta(hours=5)).date()


def _norm(s) -> str:
    return (s or "").strip().upper()


def reconciliar_posdat_plan(dbf_banc0: list[dict], pc_banc0: list[dict]) -> dict:
    """Calcula el plan (puro, testeable).

    Pareo por CLAVE ESTABLE (prov, concepto) — no por importe ordenado. El
    concepto del DBF lleva la referencia del cheque (doc/proveedor), y es
    único para ~95% de las filas; sólo colisiona en grupos con concepto vacío
    (p.ej. prov 'TJ'), donde caemos al fallback de importe ordenado dentro del
    grupo. Esto evita los falsos UPDATE/DELETE que tenía el pareo por importe
    cuando aparecían cheques nuevos (TMT 2026-06-08).

    YY **y RT** (IVA) son provisiones display-time → siempre se les fija
    importe=dBase + baseline=hoy (yy=True). El resto son cheques reales.

    dbf_banc0: [{prov, importe, concepto}]  (banc != 9 del POSDAT.DBF)
    pc_banc0:  [{id_posdat, prov, importe, concepto, linked}]
    Returns: {updates:[{id,importe,concepto,yy,que}], deletes:[{id,prov,importe,concepto,linked}],
              inserts:[{prov,importe,concepto}]}
    """
    updates: list[dict] = []
    deletes: list[dict] = []
    inserts: list[dict] = []

    def is_yy_like(p):
        # RT (IVA) acumula display-time igual que YY → mismo tratamiento.
        return _norm(p) in ("YY", "RT")

    # ── YY/RT: provisiones. Match por CLAVE CANÓNICA (prefijo MENU.PRG) ──
    # TMT 2026-06-10: antes era (prov, concepto) exacto → si la dueña editaba
    # el concepto en PC, la fila dejaba de matchear y el reconcile la ANULABA
    # + re-insertaba la del dBase (id nuevo, links rotos). Con la clave por
    # prefijo ("A,E,C...", "SUELDOS...", etc.) la identidad sobrevive
    # ediciones cosméticas. Colisión de clave (2 filas mismo prefijo) cae al
    # concepto exacto para esas filas.
    from modules.posdat.queries import clave_canonica_yy

    def _k_yy(r):
        return clave_canonica_yy(r.get("prov"), r.get("concepto"))

    # detectar claves ambiguas (aparecen 2+ veces en dbf o en pc)
    from collections import Counter
    _cnt = Counter()
    for r in dbf_banc0:
        if is_yy_like(r["prov"]):
            _cnt[_k_yy(r)] += 1
    for r in pc_banc0:
        if is_yy_like(r["prov"]):
            _cnt[_k_yy(r)] += 0  # solo dbf define ambigüedad de inserción
    _pc_cnt = Counter(_k_yy(r) for r in pc_banc0 if is_yy_like(r["prov"]))
    ambiguas = {k for k, n in _cnt.items() if n > 1} | {k for k, n in _pc_cnt.items() if n > 1}

    def _key(r):
        k = _k_yy(r)
        if k in ambiguas:
            return (_norm(r["prov"]), _norm(r["concepto"]))
        return k

    dbf_yy: dict = {}
    for r in dbf_banc0:
        if is_yy_like(r["prov"]):
            dbf_yy[_key(r)] = r
    used: set = set()
    # TMT 2026-07-07: DEDUP. Si PC tiene 2+ filas con la MISMA clave YY (pasó
    # por un full-sync que duplicó), la 1ra matchea->UPDATE y las demás->DELETE.
    # Ordenamos linkeadas primero para conservar la que tiene el mov_doble.
    _pc_yy = sorted((r for r in pc_banc0 if is_yy_like(r["prov"])),
                    key=lambda r: 0 if r.get("linked") else 1)
    for r in _pc_yy:
        k = _key(r)
        if k in dbf_yy and k not in used:
            used.add(k)
            d = dbf_yy[k]
            updates.append({"id": r["id_posdat"], "importe": round(float(d["importe"]), 2),
                            "concepto": r["concepto"], "yy": True,
                            "que": f"{k[0]} {k[1] or '(sin concepto)'}"})
        else:
            # no está en dBase, o es DUPLICADO de una clave ya matcheada -> borrar
            deletes.append({"id": r["id_posdat"], "prov": _norm(r["prov"]),
                            "importe": round(float(r["importe"]), 2),
                            "concepto": r.get("concepto"), "linked": bool(r.get("linked"))})
    for k, d in dbf_yy.items():
        if k not in used:
            inserts.append({"prov": k[0], "importe": round(float(d["importe"]), 2),
                            "concepto": d["concepto"]})

    # ── cheques reales: match por (prov, importe) MULTISET ──
    # El importe es la identidad económica estable del cheque. El concepto del
    # DBF lleva un contador que cambia día a día (p.ej. "6023  4" → "6023  3"),
    # así que NO sirve como clave (churn). Tampoco el pareo por importe-ordenado
    # -por-posición (mis-pareaba al entrar cheques nuevos). Multiset por importe:
    # los importes que coinciden matchean (no-op); los que sobran en dBase →
    # INSERT, los que sobran en PC → DELETE. Sin UPDATE (cambio de importe =
    # delete+insert, raro). TMT 2026-06-08.
    dbf_n: dict = defaultdict(list)
    pc_n: dict = defaultdict(list)
    for r in dbf_banc0:
        if not is_yy_like(r["prov"]):
            dbf_n[(_norm(r["prov"]), round(float(r["importe"]), 2))].append(r)
    for r in pc_banc0:
        if not is_yy_like(r["prov"]):
            pc_n[(_norm(r["prov"]), round(float(r["importe"]), 2))].append(r)
    for key in sorted(set(list(dbf_n) + list(pc_n))):
        prov, amt = key
        d = dbf_n.get(key, [])
        p = pc_n.get(key, [])
        n = min(len(d), len(p))
        # Las primeras n filas matchean por (prov, importe) → no-op.
        for i in range(n, len(p)):  # PC tiene de más → borrar
            deletes.append({"id": p[i]["id_posdat"], "prov": prov, "importe": amt,
                            "concepto": p[i].get("concepto"), "linked": bool(p[i].get("linked"))})
        for i in range(n, len(d)):  # dBase tiene de más → insertar (cheque nuevo)
            inserts.append({"prov": prov, "importe": amt, "concepto": d[i].get("concepto")})
    return {"updates": updates, "deletes": deletes, "inserts": inserts}


def _leer_dbf_banc0(dbf_path: Path) -> list[dict]:
    import dbfread
    out = []
    for r in dbfread.DBF(str(dbf_path), char_decode_errors="replace", load=False):
        if r.get("BANC") == 9:
            continue
        out.append({"prov": (r.get("PROV") or "").strip(),
                    "importe": round(float(r.get("IMPORTE") or 0), 2),
                    "concepto": (r.get("CONCEPTO") or "").strip()})
    return out


def _leer_pc_banc0() -> list[dict]:
    import db
    rows = db.fetch_all(
        """
        SELECT p.id_posdat, p.prov, p.importe, p.concepto,
               EXISTS (
                   SELECT 1 FROM scintela.mov_doble m
                    WHERE m.estado = 'activo'
                      AND ((m.destino_table = 'posdat' AND m.destino_id = p.id_posdat)
                        OR (m.origen_table  = 'posdat' AND m.origen_id  = p.id_posdat))
               ) AS linked
          FROM scintela.posdat p
         WHERE COALESCE(p.banc, 0) = 0
           AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
        """
    ) or []
    return [{"id_posdat": r["id_posdat"], "prov": r["prov"],
             "importe": round(float(r["importe"] or 0), 2),
             "concepto": r["concepto"], "linked": bool(r["linked"])} for r in rows]


FORM = """
<!doctype html><meta charset=utf-8><title>Reconciliar POSDAT</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>Reconciliar POSDAT con el dBase</h2>
<p>Subí el tarball con POSDAT.DBF (el mismo de /admin/dbase-sync). Corre en
<b>DRY-RUN</b> (no toca nada) salvo que marques Aplicar.</p>
<form method=post action="/admin/posdat-reconcile/run" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz" required><br><br>
  <label><input type=checkbox name=apply value=1> Aplicar (escribe en producción)</label><br><br>
  <button type=submit>Correr</button>
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
    aplicar = request.form.get("apply") in ("1", "true", "on")

    TARBALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TARBALL_PATH.exists():
        TARBALL_PATH.unlink()
    f.save(TARBALL_PATH)
    if TARBALL_PATH.stat().st_size > MAX_TARBALL_BYTES:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response("ERROR: tarball muy grande.\n", mimetype="text/plain", status=400)

    return Response(stream_with_context(_run(aplicar)), mimetype="text/plain")


def _run(aplicar: bool):
    import shutil

    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== Reconciliar POSDAT — {'APLICAR' if aplicar else 'DRY-RUN'} ===")

    # Extraer SOLO POSDAT.DBF.
    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            miembro = None
            for m in tar.getmembers():
                if m.isfile() and Path(m.name).name.upper() == "POSDAT.DBF":
                    m.name = "POSDAT.DBF"
                    tar.extract(m, EXTRACT_DIR)
                    miembro = EXTRACT_DIR / "POSDAT.DBF"
                    break
        if not miembro or not miembro.exists():
            yield line("[ERROR] el tarball no contiene POSDAT.DBF")
            return
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return

    yield from reconcile_desde_dbf(miembro, aplicar, soft_delete=False)


# ─────────────────────────────────────────────────────────────────────
# TMT 2026-07-07 (dueña): "el gráfico de flujo tiene que copiar el dBase".
# POSDAT.DBF está vetado del sync (NEVER_EXTRACT) porque el TRUNCATE+INSERT
# rompería el estado PC (id_posdat, YY, anulada, OP retiros). PERO eso deja
# la FECHAD de los posdatados VIEJA: el dBase posterga pagos a futuro y PC
# los sigue mostrando vencidos → el flujo los amontona en hoy (−2M en vez de
# +638K). Este endpoint es QUIRÚRGICO y SEGURO: refresca SOLO la columna
# `fechad` (banc 0 y 9) desde el POSDAT.DBF fresco, pareando por
# prov+importe+concepto. NO inserta, NO borra → cero riesgo de doble conteo o
# pérdida de datos. Dry-run por defecto; aplica con ?apply=1.
# ─────────────────────────────────────────────────────────────────────

def _norm_cpt(s) -> str:
    return " ".join((s or "").strip().upper().split())


def _leer_dbf_fechad(dbf_path):
    """[{prov, importe, concepto, fechad}] de POSDAT.DBF (num!=9999, con fechad)."""
    import dbfread
    out = []
    for r in dbfread.DBF(str(dbf_path), char_decode_errors="replace", load=False):
        try:
            if int(r.get("NUM") or 0) == 9999:
                continue
        except (TypeError, ValueError):
            pass
        fd = r.get("FECHAD")
        if not fd:
            continue
        out.append({
            "prov": (r.get("PROV") or "").strip().upper(),
            "importe": round(float(r.get("IMPORTE") or 0), 2),
            "concepto": _norm_cpt(r.get("CONCEPTO")),
            "fechad": fd,
        })
    return out


_FORM_FECHAD = """
<!doctype html><meta charset=utf-8><title>POSDAT fechad-sync</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>Refrescar fechad de POSDAT desde el dBase</h2>
<p>Sub&iacute; el tarball con POSDAT.DBF fresco. Actualiza <b>SOLO</b> la
columna <code>fechad</code> (banc 0 y 9) pareando por prov+importe+concepto.
No inserta ni borra. Corre en <b>DRY-RUN</b> salvo que marques Aplicar.</p>
<form method=post action="/admin/posdat-reconcile/fechad-sync" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz" required><br><br>
  <label><input type=checkbox name=apply value=1> Aplicar (escribe en producci&oacute;n)</label><br><br>
  <button type=submit>Correr</button>
</form></div>
"""


@bp.route("/fechad-sync", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def fechad_sync():
    if request.method == "GET":
        return render_template_string(_FORM_FECHAD)
    f = request.files.get("tarball")
    if not f or not f.filename:
        return Response("ERROR: falta el tarball.\n", mimetype="text/plain", status=400)
    if not f.filename.lower().endswith((".tar.gz", ".tgz")):
        return Response("ERROR: esperaba .tar.gz / .tgz.\n", mimetype="text/plain", status=400)
    aplicar = request.form.get("apply") in ("1", "true", "on")
    TARBALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TARBALL_PATH.exists():
        TARBALL_PATH.unlink()
    f.save(TARBALL_PATH)
    if TARBALL_PATH.stat().st_size > MAX_TARBALL_BYTES:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response("ERROR: tarball muy grande.\n", mimetype="text/plain", status=400)
    return Response(stream_with_context(_run_fechad(aplicar)), mimetype="text/plain")


def _run_fechad(aplicar: bool):
    import shutil
    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== POSDAT fechad-sync — {'APLICAR' if aplicar else 'DRY-RUN'} ===")
    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        miembro = None
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and Path(m.name).name.upper() == "POSDAT.DBF":
                    m.name = "POSDAT.DBF"
                    tar.extract(m, EXTRACT_DIR)
                    miembro = EXTRACT_DIR / "POSDAT.DBF"
                    break
        if not miembro or not miembro.exists():
            yield line("[ERROR] el tarball no contiene POSDAT.DBF")
            return
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return

    dbf = _leer_dbf_fechad(miembro)
    yield line(f"POSDAT.DBF: {len(dbf)} registros con fechad (num!=9999).")

    # Registros DBF disponibles para consumir 1 a 1. Dos índices:
    #   exact = (prov, importe, concepto)  ·  loose = (prov, importe)
    # Los posdat de importación de PC (andres) tienen concepto NULL → no
    # matchean por concepto; el 2do pase por (prov,importe) los alcanza.
    import itertools as _it
    dbf_recs = [{"prov": d["prov"], "importe": d["importe"],
                 "concepto": d["concepto"], "fechad": d["fechad"], "used": False}
                for d in dbf]
    idx_exact = defaultdict(list)
    idx_loose = defaultdict(list)
    for rec in dbf_recs:
        idx_exact[(rec["prov"], rec["importe"], rec["concepto"])].append(rec)
        idx_loose[(rec["prov"], rec["importe"])].append(rec)
    for k in idx_exact: idx_exact[k].sort(key=lambda r: r["fechad"])
    for k in idx_loose: idx_loose[k].sort(key=lambda r: r["fechad"])

    def _tomar(prov, imp, cpt):
        for rec in idx_exact.get((prov, imp, cpt), ()):  # 1er pase: exacto
            if not rec["used"]:
                rec["used"] = True
                return rec["fechad"]
        for rec in idx_loose.get((prov, imp), ()):        # 2do pase: prov+importe
            if not rec["used"]:
                rec["used"] = True
                return rec["fechad"]
        return None

    pc = db.fetch_all(
        """
        SELECT id_posdat, COALESCE(prov,'') AS prov, importe,
               COALESCE(concepto,'') AS concepto, fechad, COALESCE(banc,0) AS banc
          FROM scintela.posdat
         WHERE (anulada IS NOT TRUE OR anulada IS NULL)
           AND COALESCE(num, 0) <> 9999
         ORDER BY id_posdat
        """
    ) or []
    yield line(f"PC posdat vivos (num!=9999): {len(pc)}")

    cambios = []
    sin_match = 0
    for r in pc:
        prov = r["prov"].strip().upper(); imp = round(float(r["importe"] or 0), 2)
        nueva = _tomar(prov, imp, _norm_cpt(r["concepto"]))
        if nueva is None:
            sin_match += 1
            continue
        vieja = r["fechad"]
        if vieja != nueva:
            cambios.append((r["id_posdat"], r["prov"], imp, vieja, nueva))

    yield line(f"Pareados con cambio de fechad: {len(cambios)} · sin match en DBF: {sin_match}")
    yield line("")
    for cid, prov, imp, vieja, nueva in cambios[:60]:
        yield line(f"  #{cid} {prov:<4} {imp:>12,.2f}  {vieja} -> {nueva}")
    if len(cambios) > 60:
        yield line(f"  ... (+{len(cambios) - 60} más)")
    yield line("")

    if not aplicar:
        yield line(">>> DRY-RUN: no se tocó nada. Reenviá con Aplicar para escribir.")
        return

    n = 0
    try:
        with db.tx() as conn:
            for cid, _p, _i, _v, nueva in cambios:
                db.execute(
                    "UPDATE scintela.posdat SET fechad = %s WHERE id_posdat = %s",
                    (nueva, cid), conn=conn,
                )
                n += 1
        yield line(f">>> APLICADO: {n} fechad actualizadas.")
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] al aplicar (rollback): {exc!r}")


def reconcile_desde_dbf(dbf_path: Path, aplicar: bool, soft_delete: bool = False):
    """Lee POSDAT.DBF + PC, calcula el plan, lo loguea y (si aplicar) lo ejecuta.

    Reusable: lo llama el endpoint manual /admin/posdat-reconcile (soft_delete=
    False → DELETE real para no-linkeadas) y el pipeline de /admin/dbase-sync
    (soft_delete=True → ANULA en vez de borrar, recuperable, para el auto-run
    en cada sync). TMT 2026-06-08.

    Guard de seguridad: si `aplicar` y el DBF trae <50% de las filas que tiene
    PC (dump parcial/corrupto), ABORTA — no queremos borrar/anular en masa por
    un archivo malo.
    """
    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    # TMT 2026-06-10: baseline = FECHA DEL SNAPSHOT (mtime del DBF), no
    # "hoy". Si el tarball es de ayer y se aplica hoy, pinear con
    # baseline=hoy perdía los días intermedios de cuota — el persist
    # arrancaba desde hoy con el valor de ayer. Con la fecha real del
    # archivo, el próximo persist acumula snapshot→hoy y queda alineado.
    try:
        snapshot_date = (datetime.utcfromtimestamp(dbf_path.stat().st_mtime)
                         - timedelta(hours=5)).date()
    except Exception:  # noqa: BLE001
        snapshot_date = _hoy_ec()
    if snapshot_date > _hoy_ec():
        snapshot_date = _hoy_ec()
    dbf = _leer_dbf_banc0(dbf_path)
    pc = _leer_pc_banc0()
    yield line(f"Snapshot DBF fechado {snapshot_date} (mtime) — baseline YY/RT se pinea a esa fecha")
    if (_hoy_ec() - snapshot_date).days > 7:
        yield line(f"  ⚠ snapshot de hace {(_hoy_ec() - snapshot_date).days} días — ¿tarball viejo? (CloudShell no sobreescribe: rm antes de subir)")
    yield line(f"DBF banc<>9: {len(dbf)} filas, suma {sum(x['importe'] for x in dbf):,.2f}")
    yield line(f"PC  banc=0 : {len(pc)} filas, suma {sum(x['importe'] for x in pc):,.2f}")
    yield line("")

    # ── Guard anti-borrado masivo ──
    if aplicar and len(pc) > 0 and len(dbf) < 0.5 * len(pc):
        yield line(f"[ABORT] el DBF trae {len(dbf)} filas vs {len(pc)} en PC "
                   f"(<50%). Parece un dump parcial — no aplico para no borrar en masa.")
        return

    plan = reconciliar_posdat_plan(dbf, pc)
    ups, dels, ins = plan["updates"], plan["deletes"], plan["inserts"]
    linked_dels = [d for d in dels if d["linked"]]

    yield line(f"PLAN: {len(ups)} UPDATE · {len(dels)} DELETE · {len(ins)} INSERT")
    if linked_dels:
        yield line(f"  ⚠ {len(linked_dels)} con link de compra (mov_doble) → se ANULAN (preservan id/link).")
    yield line("")
    yield line("--- UPDATE (conserva id) ---")
    for u in ups:
        yield line(f"  id={u['id']:<6} {u['que']:18} -> {u['importe']:>12,.2f}"
                   + ("  [baseline=hoy]" if u["yy"] else ""))
    accion_del = "ANULA" if soft_delete else "DELETE"
    yield line(f"--- {accion_del} (PC tiene de más) ---")
    for d in dels:
        yield line(f"  id={d['id']:<6} {d['prov']:4} {d['importe']:>12,.2f}"
                   + ("  ⚠LINKED→ANULA" if d["linked"] else f"  {accion_del}"))
    yield line("--- INSERT (falta en PC) ---")
    for i in ins:
        yield line(f"  {i['prov']:4} {i['importe']:>12,.2f}  {(i['concepto'] or '')[:24]}")
    yield line("")

    # Sum proyectada post-plan.
    proj = sum(x["importe"] for x in pc)
    for u in ups:
        old = next((r["importe"] for r in pc if r["id_posdat"] == u["id"]), 0)
        proj += u["importe"] - old
    for d in dels:
        proj -= d["importe"]
    for i in ins:
        proj += i["importe"]
    dbf_sum = sum(x["importe"] for x in dbf)
    yield line(f"Suma PC proyectada: {proj:,.2f}   |   dBase: {dbf_sum:,.2f}   |   diff: {proj - dbf_sum:,.2f}")
    yield line("")

    if not aplicar:
        yield line("DRY-RUN: no se tocó nada. Marcá 'Aplicar' para ejecutar.")
        return

    # ── APLICAR en una transacción ──
    hoy = snapshot_date  # baseline de YY/RT = fecha del snapshot (ver arriba)
    n_up = n_del = n_ins = n_anul = 0
    motivo = ("reconcile-sync: no está en POSDAT.DBF"
              if soft_delete else
              "reconcile-dbf: no está en POSDAT.DBF; anulada (tiene link de compra)")
    try:
        with db.tx() as conn:
            for u in ups:
                if u["yy"]:
                    db.execute(
                        "UPDATE scintela.posdat SET importe=%s, baseline_date=%s WHERE id_posdat=%s",
                        (u["importe"], hoy, u["id"]), conn=conn)
                else:
                    db.execute(
                        "UPDATE scintela.posdat SET importe=%s WHERE id_posdat=%s",
                        (u["importe"], u["id"]), conn=conn)
                n_up += 1
            for d in dels:
                # soft_delete=True → SIEMPRE anula (recuperable). Si no, sólo las
                # linkeadas se anulan (no se pueden borrar) y el resto se borra.
                if soft_delete or d["linked"]:
                    db.execute(
                        "UPDATE scintela.posdat SET anulada=TRUE, "
                        "motivo_anulacion=%s, fecha_anulacion=NOW() WHERE id_posdat=%s",
                        (motivo, d["id"]), conn=conn)
                    n_anul += 1
                else:
                    db.execute("DELETE FROM scintela.posdat WHERE id_posdat=%s", (d["id"],), conn=conn)
                    n_del += 1
            for i in ins:
                # YY/RT nuevas nacen con baseline = snapshot, así el persist
                # las acumula desde el día correcto y el cron legacy
                # (baseline IS NULL) jamás las toca. TMT 2026-06-10.
                if _norm(i["prov"]) in ("YY", "RT"):
                    db.execute(
                        "INSERT INTO scintela.posdat (prov, importe, concepto, banc, usuario_crea, baseline_date) "
                        "VALUES (%s, %s, %s, 0, %s, %s)",
                        (i["prov"], i["importe"], i["concepto"], "reconcile-dbf", hoy), conn=conn)
                else:
                    db.execute(
                        "INSERT INTO scintela.posdat (prov, importe, concepto, banc, usuario_crea) "
                        "VALUES (%s, %s, %s, 0, %s)",
                        (i["prov"], i["importe"], i["concepto"], "reconcile-dbf"), conn=conn)
                n_ins += 1
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] rollback — {exc!r}")
        return

    yield line(f"APLICADO ✓ — {n_up} UPDATE, {n_del} DELETE, {n_anul} ANULADAS, {n_ins} INSERT.")
    nuevo = _leer_pc_banc0()
    yield line(f"PC banc=0 ahora: {len(nuevo)} filas, suma {sum(x['importe'] for x in nuevo):,.2f}")
    yield line(f"dBase: {dbf_sum:,.2f}")


# ─────────────────────────────────────────────────────────────────────
# TMT 2026-07-07 (dueña): "uno por uno" — dump JSON de posdat de PC para
# comparar contra POSDAT.DBF fuera de la app, + apply de un mapa exacto
# id_posdat->fechad (yo calculo el match preciso con el dBase). Solo fechad.
# ─────────────────────────────────────────────────────────────────────

@bp.route("/pc-dump", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def pc_dump():
    import db
    from flask import jsonify
    rows = db.fetch_all(
        """
        SELECT id_posdat, COALESCE(num,0) AS num, COALESCE(prov,'') AS prov,
               importe, COALESCE(concepto,'') AS concepto, fechad,
               COALESCE(banc,0) AS banc, COALESCE(usuario_crea,'') AS usuario_crea
          FROM scintela.posdat
         WHERE (anulada IS NOT TRUE OR anulada IS NULL)
           AND COALESCE(num,0) <> 9999
         ORDER BY id_posdat
        """
    ) or []
    out = []
    for r in rows:
        out.append({
            "id": int(r["id_posdat"]),
            "num": int(r["num"] or 0),
            "prov": r["prov"],
            "importe": round(float(r["importe"] or 0), 2),
            "concepto": r["concepto"] or "",
            "fechad": r["fechad"].isoformat() if r["fechad"] else None,
            "banc": int(r["banc"] or 0),
            "usuario_crea": r["usuario_crea"],
        })
    return jsonify(out)


@bp.route("/apply-fechad", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def apply_fechad():
    """Aplica un mapa exacto {id_posdat: 'YYYY-MM-DD' | null} a fechad. SOLO fechad."""
    import json as _json
    from datetime import date as _date
    import db
    raw = request.form.get("mapa") or "{}"
    try:
        mapa = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return Response(f"ERROR mapa JSON: {e}\n", mimetype="text/plain", status=400)
    aplicar = request.form.get("apply") in ("1", "true", "on")

    def _run():
        n = 0
        cambios = []
        for k, v in mapa.items():
            try:
                idp = int(k)
            except (TypeError, ValueError):
                continue
            nueva = None
            if v:
                try:
                    y, m, d = str(v).split("-")
                    nueva = _date(int(y), int(m), int(d))
                except Exception:  # noqa: BLE001
                    yield f"[skip] id={idp} fecha inválida {v!r}\n"
                    continue
            cambios.append((idp, nueva))
        yield f"=== apply-fechad — {'APLICAR' if aplicar else 'DRY-RUN'} · {len(cambios)} ids ===\n"
        if not aplicar:
            for idp, nueva in cambios[:80]:
                yield f"  id={idp} -> {nueva}\n"
            yield ">>> DRY-RUN.\n"
            return
        try:
            with db.tx() as conn:
                for idp, nueva in cambios:
                    db.execute("UPDATE scintela.posdat SET fechad=%s WHERE id_posdat=%s",
                               (nueva, idp), conn=conn)
                    n += 1
            yield f">>> APLICADO: {n} fechad actualizadas.\n"
        except Exception as e:  # noqa: BLE001
            yield f"[ERROR rollback] {e!r}\n"

    return Response(stream_with_context(_run()), mimetype="text/plain")


# ─────────────────────────────────────────────────────────────────────
# TMT 2026-07-07 (dueña): "que quede TODO igual al dBase". Los posdat de
# importación (banc=9, AC/AI/CL) y YY vienen de 'dbf-import' pero con IMPORTES
# viejos — el dBase los actualizó y PC nunca los sincronizó (POSDAT vetado).
# Este endpoint RESTAURA el sync de posdat SOLO para los registros de origen
# dBase: borra los usuario_crea IN ('dbf-import','reconcile-dbf') que NO tengan
# link mov_doble (los dbf-import puros no tienen estado propio de PC), e inserta
# TODO el POSDAT.DBF fresco. PRESERVA los PC-creados (andres/tamara/pc-retiro-op
# y cualquiera con link). Dry-run por defecto; apply=1 en una transacción.
# ─────────────────────────────────────────────────────────────────────

_FORM_FULL = """
<!doctype html><meta charset=utf-8><title>POSDAT full-sync</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>Sincronizar POSDAT con el dBase (registros dBase)</h2>
<p>Sub&iacute; el tarball con POSDAT.DBF fresco. <b>Borra</b> los posdat de origen
dBase (dbf-import / reconcile-dbf) SIN link mov_doble e <b>inserta</b> el POSDAT.DBF
completo. <b>Preserva</b> los PC-creados (andres/tamara/OP y los que tengan link).
DRY-RUN salvo que marques Aplicar.</p>
<form method=post action="/admin/posdat-reconcile/full-sync" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz" required><br><br>
  <label><input type=checkbox name=apply value=1> Aplicar (escribe en producci&oacute;n)</label><br><br>
  <button type=submit>Correr</button>
</form></div>
"""


def _leer_dbf_full(dbf_path):
    """Todos los registros de POSDAT.DBF (num!=9999) con todos los campos."""
    import dbfread
    out = []
    for r in dbfread.DBF(str(dbf_path), char_decode_errors="replace", load=False):
        try:
            if int(r.get("NUM") or 0) == 9999:
                continue
        except (TypeError, ValueError):
            pass
        out.append({
            "fecha": r.get("FECHA"),
            "fechad": r.get("FECHAD"),
            "prov": (r.get("PROV") or "").strip()[:3],
            "num": int(r.get("NUM") or 0),
            "importe": round(float(r.get("IMPORTE") or 0), 2),
            "concepto": (r.get("CONCEPTO") or "").strip()[:100],
            "banc": int(r.get("BANC") or 0),
            "clave": (r.get("CLAVE") or "").strip()[:3],
        })
    return out


@bp.route("/full-sync", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def full_sync():
    if request.method == "GET":
        return render_template_string(_FORM_FULL)
    aplicar = request.form.get("apply") in ("1", "true", "on")
    # TMT 2026-07-08: modo sin-streaming. La respuesta chunked se corta a través
    # del proxy (el cliente ve "network error" tras la 1ra línea). Con nostream=1
    # juntamos toda la salida server-side y la devolvemos de una — confiable para
    # leer el plan y para APLICAR sin que un corte de conexión mate la tx.
    nostream = request.form.get("nostream") in ("1", "true", "on")

    # TMT 2026-07-08: entrada por JSON (base64 de gzip de una lista de filas con
    # los mismos campos que _leer_dbf_full). Evita el parseo de POSDAT.DBF con
    # dbfread, que difiere entre plataformas (el DBF regenerado en Linux se leía
    # corrido / con importes inflados en el dbfread de Windows del server).
    pj = request.form.get("posdat_json")
    if pj:
        import base64
        import gzip
        import json as _json
        try:
            raw = base64.b64decode(pj)
            try:
                raw = gzip.decompress(raw)
            except Exception:  # noqa: BLE001
                pass  # permitir JSON sin gzip
            rows = _json.loads(raw.decode("utf-8"))
            if not isinstance(rows, list):
                raise ValueError("posdat_json debe ser una lista")
        except Exception as exc:  # noqa: BLE001
            return Response(f"ERROR: posdat_json inválido: {exc!r}\n",
                            mimetype="text/plain", status=400)
        gen = _run_full_json(rows, aplicar)
        if nostream:
            return Response("".join(gen), mimetype="text/plain")
        return Response(stream_with_context(gen), mimetype="text/plain")

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
    if nostream:
        return Response("".join(_run_full(aplicar)), mimetype="text/plain")
    return Response(stream_with_context(_run_full(aplicar)), mimetype="text/plain")


def _run_full(aplicar: bool):
    import shutil
    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== POSDAT full-sync — {'APLICAR' if aplicar else 'DRY-RUN'} ===")
    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        miembro = None
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and Path(m.name).name.upper() == "POSDAT.DBF":
                    m.name = "POSDAT.DBF"
                    tar.extract(m, EXTRACT_DIR)
                    miembro = EXTRACT_DIR / "POSDAT.DBF"
                    break
        if not miembro or not miembro.exists():
            yield line("[ERROR] el tarball no contiene POSDAT.DBF")
            return
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return

    # TMT 2026-07-08: capturar cualquier excepción del reconcile y devolverla
    # como texto (antes tiraba 500 en nostream / cortaba el stream). Así el
    # dry-run muestra la causa en vez de una página de error opaca.
    try:
        yield from reconcile_posdat_full_desde_dbf(miembro, aplicar)
    except Exception as exc:  # noqa: BLE001
        import traceback
        yield line(f"[ERROR reconcile] {exc!r}")
        yield line(traceback.format_exc())


def _run_full_json(rows: list, aplicar: bool):
    """Igual que _run_full pero desde filas JSON (sin POSDAT.DBF). Normaliza los
    campos al shape de _leer_dbf_full y corre el mismo reconcile."""
    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== POSDAT full-sync (JSON) — {'APLICAR' if aplicar else 'DRY-RUN'} ===")
    try:
        norm = []
        for r in rows:
            norm.append({
                "fecha": r.get("fecha"),
                "fechad": r.get("fechad"),
                "prov": (str(r.get("prov") or "").strip())[:3],
                "num": int(r.get("num") or 0),
                "importe": round(float(r.get("importe") or 0), 2),
                "concepto": (str(r.get("concepto") or "").strip())[:100],
                "banc": int(r.get("banc") or 0),
                "clave": (str(r.get("clave") or "").strip())[:3],
            })
        yield from reconcile_posdat_full_desde_dbf(None, aplicar, dbf_rows=norm)
    except Exception as exc:  # noqa: BLE001
        import traceback
        yield line(f"[ERROR reconcile-json] {exc!r}")
        yield line(traceback.format_exc())


def reconcile_posdat_full_desde_dbf(dbf_path, aplicar: bool, soft_delete: bool = False,
                                    dbf_rows: list | None = None):
    """Reconcile de scintela.posdat contra POSDAT.DBF (banc 0 y 9).

    banc=0 -> reconciliar_posdat_plan: match por clave canónica YY/RT con
    UPDATE in-place (PRESERVA el mov_doble link), cheques por multiset
    prov+importe, y DEDUP de duplicados. banc=9 (importaciones) -> borra los
    dbf-import/reconcile-dbf sin link e inserta el DBF (dedup exacto). Así el
    pasivo (banc=0) queda IGUAL al dBase sin romper links ni duplicar.
    TMT 2026-07-07 (dueña "alinear TODO al dBase" + arreglo utilidad -1M).
    Reusable: endpoint /full-sync + hook post-sync de /admin/dbase-sync.

    TMT 2026-07-08: `dbf_rows` opcional — lista ya parseada (mismos campos que
    _leer_dbf_full: fecha, fechad, prov, num, importe, concepto, banc, clave).
    Se usa cuando los datos llegan por JSON en vez de un POSDAT.DBF, para
    evitar diferencias de parseo dbfread entre plataformas.
    """
    from collections import Counter as _Counter
    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    if dbf_rows is not None:
        dbf = [d for d in dbf_rows if int(d.get("num") or 0) != 9999]
    else:
        dbf = _leer_dbf_full(dbf_path)
    dbf_b0 = [{"prov": d["prov"], "importe": d["importe"], "concepto": d["concepto"]}
              for d in dbf if int(d["banc"]) == 0]
    dbf_b9 = [d for d in dbf if int(d["banc"]) == 9]
    origen = "JSON" if dbf_rows is not None else "POSDAT.DBF"
    yield line(f"[posdat-reconcile] {origen}: {len(dbf)} regs (banc0={len(dbf_b0)}, banc9={len(dbf_b9)}).")

    # baseline YY/RT = fecha del snapshot (mtime del DBF, o HOY si viene por JSON).
    if dbf_rows is not None:
        snap = _hoy_ec()
    else:
        try:
            snap = (datetime.utcfromtimestamp(dbf_path.stat().st_mtime) - timedelta(hours=5)).date()
        except Exception:  # noqa: BLE001
            snap = _hoy_ec()
    if snap > _hoy_ec():
        snap = _hoy_ec()

    # ── BANC=0: plan correcto (UPDATE in-place preserva links + dedup) ──
    pc_b0 = _leer_pc_banc0()
    if aplicar and len(pc_b0) > 0 and len(dbf_b0) < 0.5 * len(pc_b0):
        yield line(f"[ABORT] DBF banc0 {len(dbf_b0)} vs PC {len(pc_b0)} (<50%) — dump parcial, no aplico.")
        return
    plan = reconciliar_posdat_plan(dbf_b0, pc_b0)
    ups, dels, ins = plan["updates"], plan["deletes"], plan["inserts"]

    proj_b0 = sum(x["importe"] for x in pc_b0)
    for u in ups:
        old_imp = next((r["importe"] for r in pc_b0 if r["id_posdat"] == u["id"]), 0)
        proj_b0 += u["importe"] - old_imp
    for d in dels:
        proj_b0 -= d["importe"]
    for i in ins:
        proj_b0 += i["importe"]
    dbf_b0_sum = sum(x["importe"] for x in dbf_b0)

    # ── BANC=9: borrar dbf-import/reconcile-dbf sin link + insert DBF (dedup) ──
    _LINK = ("EXISTS (SELECT 1 FROM scintela.mov_doble m WHERE m.estado='activo' "
             "AND ((m.destino_table='posdat' AND m.destino_id=p.id_posdat) "
             "OR (m.origen_table='posdat' AND m.origen_id=p.id_posdat)))")
    a_borrar9 = db.fetch_all(
        f"""
        SELECT p.id_posdat, {_LINK} AS linked
          FROM scintela.posdat p
         WHERE COALESCE(p.num,0) <> 9999
           AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
           AND COALESCE(p.banc,0) = 9
           AND COALESCE(p.usuario_crea,'') IN ('dbf-import','reconcile-dbf')
        """
    ) or []
    borrar9_ids = [r["id_posdat"] for r in a_borrar9 if not r["linked"]]
    quedan9 = db.fetch_all(
        f"""
        SELECT COALESCE(p.prov,'') AS prov, p.importe, COALESCE(p.concepto,'') AS concepto
          FROM scintela.posdat p
         WHERE COALESCE(p.num,0) <> 9999
           AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
           AND COALESCE(p.banc,0) = 9
           AND ( COALESCE(p.usuario_crea,'') NOT IN ('dbf-import','reconcile-dbf') OR {_LINK} )
        """
    ) or []
    keys9 = _Counter()
    for q in quedan9:
        keys9[((q["prov"] or "").strip().upper(), round(float(q["importe"] or 0), 2), _norm_cpt(q["concepto"]))] += 1
    ins9 = []
    for d in dbf_b9:
        k = ((d["prov"] or "").strip().upper(), d["importe"], _norm_cpt(d["concepto"]))
        if keys9.get(k, 0) > 0:
            keys9[k] -= 1
        else:
            ins9.append(d)

    # ── REPORTE ──
    yield line(f"[posdat-reconcile] BANC=0 plan: {len(ups)} UPDATE · {len(dels)} DEL/ANULA · {len(ins)} INSERT")
    yield line(f"[posdat-reconcile] >>> PASIVO banc=0 proyectado ≈ {proj_b0:,.2f}  |  dBase {dbf_b0_sum:,.2f}  |  diff {proj_b0 - dbf_b0_sum:,.2f}")
    yield line(f"[posdat-reconcile] BANC=9: borrar {len(borrar9_ids)} · insertar {len(ins9)} de {len(dbf_b9)}")

    if not aplicar:
        yield line(">>> DRY-RUN: no se tocó nada.")
        return

    motivo = "reconcile-dbf: no está en POSDAT.DBF"
    try:
        with db.tx() as conn:
            for u in ups:
                if u["yy"]:
                    db.execute("UPDATE scintela.posdat SET importe=%s, baseline_date=%s WHERE id_posdat=%s",
                               (u["importe"], snap, u["id"]), conn=conn)
                else:
                    db.execute("UPDATE scintela.posdat SET importe=%s WHERE id_posdat=%s",
                               (u["importe"], u["id"]), conn=conn)
            for d in dels:
                if soft_delete or d["linked"]:
                    db.execute("UPDATE scintela.posdat SET anulada=TRUE, motivo_anulacion=%s, "
                               "fecha_anulacion=NOW() WHERE id_posdat=%s", (motivo, d["id"]), conn=conn)
                else:
                    db.execute("DELETE FROM scintela.posdat WHERE id_posdat=%s", (d["id"],), conn=conn)
            for i in ins:
                if _norm(i["prov"]) in ("YY", "RT"):
                    db.execute("INSERT INTO scintela.posdat (prov, importe, concepto, banc, usuario_crea, baseline_date) "
                               "VALUES (%s,%s,%s,0,'reconcile-dbf',%s)",
                               (i["prov"], i["importe"], i["concepto"], snap), conn=conn)
                else:
                    db.execute("INSERT INTO scintela.posdat (prov, importe, concepto, banc, usuario_crea) "
                               "VALUES (%s,%s,%s,0,'reconcile-dbf')",
                               (i["prov"], i["importe"], i["concepto"]), conn=conn)
            if borrar9_ids:
                db.execute("DELETE FROM scintela.posdat WHERE id_posdat = ANY(%s)", (borrar9_ids,), conn=conn)
            for d in ins9:
                db.execute("INSERT INTO scintela.posdat "
                           "(fecha, fechad, prov, num, importe, concepto, banc, clave, usuario_crea) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'dbf-import')",
                           (d["fecha"], d["fechad"], d["prov"], d["num"], d["importe"],
                            d["concepto"], d["banc"], d["clave"]), conn=conn)
        nuevo = _leer_pc_banc0()
        yield line(f">>> APLICADO ✓ banc=0: {len(ups)}U/{len(dels)}D/{len(ins)}I → suma {sum(x['importe'] for x in nuevo):,.2f} (dBase {dbf_b0_sum:,.2f})")
        yield line(f">>> APLICADO ✓ banc=9: -{len(borrar9_ids)} +{len(ins9)}")
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR posdat-reconcile rollback] {exc!r}")

