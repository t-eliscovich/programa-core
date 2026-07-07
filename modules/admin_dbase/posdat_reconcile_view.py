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
    for r in pc_banc0:
        if not is_yy_like(r["prov"]):
            continue
        k = _key(r)
        if k in dbf_yy:
            used.add(k)
            d = dbf_yy[k]
            updates.append({"id": r["id_posdat"], "importe": round(float(d["importe"]), 2),
                            "concepto": r["concepto"], "yy": True,
                            "que": f"{k[0]} {k[1] or '(sin concepto)'}"})
        else:
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

    # Mapa DBF: key -> lista de fechad (para consumir 1 a 1 en duplicados).
    dbf_map = defaultdict(list)
    for d in dbf:
        dbf_map[(d["prov"], d["importe"], d["concepto"])].append(d["fechad"])
    for k in dbf_map:
        dbf_map[k].sort()

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
        key = (r["prov"].strip().upper(), round(float(r["importe"] or 0), 2), _norm_cpt(r["concepto"]))
        fechas = dbf_map.get(key)
        if not fechas:
            sin_match += 1
            continue
        nueva = fechas.pop(0)  # consume 1
        vieja = r["fechad"]
        if vieja != nueva:
            cambios.append((r["id_posdat"], r["prov"], round(float(r["importe"] or 0), 2), vieja, nueva))

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
