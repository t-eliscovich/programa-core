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
    """Calcula el plan (puro, testeable). Pareo por prov + importe ordenado.

    dbf_banc0: [{prov, importe, concepto}]  (banc != 9 del POSDAT.DBF)
    pc_banc0:  [{id_posdat, prov, importe, concepto, linked}]
    Returns: {updates:[{id,importe,concepto,yy}], deletes:[{id,prov,importe,linked}],
              inserts:[{prov,importe,concepto}]}
    """
    updates: list[dict] = []
    deletes: list[dict] = []
    inserts: list[dict] = []

    def is_yy(p):
        return _norm(p) == "YY"

    # ── YY: match por concepto (único) ──
    dbf_yy = {}
    for r in dbf_banc0:
        if is_yy(r["prov"]):
            dbf_yy[_norm(r["concepto"])] = r
    used = set()
    for r in pc_banc0:
        if not is_yy(r["prov"]):
            continue
        c = _norm(r["concepto"])
        if c in dbf_yy:
            used.add(c)
            d = dbf_yy[c]
            # YY siempre: fija baseline=hoy y el importe del dBase.
            updates.append({"id": r["id_posdat"], "importe": round(float(d["importe"]), 2),
                            "concepto": r["concepto"], "yy": True,
                            "que": f"YY {c or '(sin concepto)'}"})
        else:
            deletes.append({"id": r["id_posdat"], "prov": "YY",
                            "importe": round(float(r["importe"]), 2),
                            "concepto": r.get("concepto"), "linked": bool(r.get("linked"))})
    for c, d in dbf_yy.items():
        if c not in used:
            inserts.append({"prov": "YY", "importe": round(float(d["importe"]), 2),
                            "concepto": d["concepto"]})

    # ── no-YY: por prov, pareo por importe ordenado ──
    dbf_n = defaultdict(list)
    pc_n = defaultdict(list)
    for r in dbf_banc0:
        if not is_yy(r["prov"]):
            dbf_n[_norm(r["prov"])].append(r)
    for r in pc_banc0:
        if not is_yy(r["prov"]):
            pc_n[_norm(r["prov"])].append(r)
    for prov in sorted(set(list(dbf_n) + list(pc_n))):
        d = sorted(dbf_n.get(prov, []), key=lambda x: float(x["importe"]))
        p = sorted(pc_n.get(prov, []), key=lambda x: float(x["importe"]))
        n = min(len(d), len(p))
        for i in range(n):
            if (abs(float(p[i]["importe"]) - float(d[i]["importe"])) > 0.005
                    or (p[i].get("concepto") or "") != (d[i].get("concepto") or "")):
                updates.append({"id": p[i]["id_posdat"], "importe": round(float(d[i]["importe"]), 2),
                                "concepto": d[i].get("concepto"), "yy": False, "que": prov})
        for i in range(n, len(p)):
            deletes.append({"id": p[i]["id_posdat"], "prov": prov,
                            "importe": round(float(p[i]["importe"]), 2),
                            "concepto": p[i].get("concepto"), "linked": bool(p[i].get("linked"))})
        for i in range(n, len(d)):
            inserts.append({"prov": prov, "importe": round(float(d[i]["importe"]), 2),
                            "concepto": d[i].get("concepto")})
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

    dbf = _leer_dbf_banc0(miembro)
    pc = _leer_pc_banc0()
    yield line(f"DBF banc<>9: {len(dbf)} filas, suma {sum(x['importe'] for x in dbf):,.2f}")
    yield line(f"PC  banc=0 : {len(pc)} filas, suma {sum(x['importe'] for x in pc):,.2f}")
    yield line("")

    plan = reconciliar_posdat_plan(dbf, pc)
    ups, dels, ins = plan["updates"], plan["deletes"], plan["inserts"]
    linked_dels = [d for d in dels if d["linked"]]

    yield line(f"PLAN: {len(ups)} UPDATE · {len(dels)} DELETE · {len(ins)} INSERT")
    if linked_dels:
        yield line(f"  ⚠ {len(linked_dels)} con link de compra (mov_doble) → se ANULAN (anulada=true: salen del Pasivos, preservan id/link).")
    yield line("")
    yield line("--- UPDATE (conserva id) ---")
    for u in ups:
        yield line(f"  id={u['id']:<6} {u['que']:18} -> {u['importe']:>12,.2f}"
                   + ("  [baseline=hoy]" if u["yy"] else ""))
    yield line("--- DELETE (PC tiene de más) ---")
    for d in dels:
        yield line(f"  id={d['id']:<6} {d['prov']:4} {d['importe']:>12,.2f}"
                   + ("  ⚠LINKED→ANULA" if d["linked"] else "  DELETE"))
    yield line("--- INSERT (falta en PC) ---")
    for i in ins:
        yield line(f"  {i['prov']:4} {i['importe']:>12,.2f}  {(i['concepto'] or '')[:24]}")
    yield line("")

    # Sum proyectada post-plan (saltando linked deletes).
    proj = sum(x["importe"] for x in pc)
    for u in ups:
        old = next((r["importe"] for r in pc if r["id_posdat"] == u["id"]), 0)
        proj += u["importe"] - old
    for d in dels:
        # linked → se anulan (salen del Pasivos igual); no-linked → delete.
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
    hoy = _hoy_ec()
    n_up = n_del = n_ins = n_anul = 0
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
                if d["linked"]:
                    # No se puede borrar (mov_doble la referencia) → anular:
                    # sale del Pasivos (filtra anulada) y preserva el id/link.
                    db.execute(
                        "UPDATE scintela.posdat SET anulada=TRUE, "
                        "motivo_anulacion=%s, fecha_anulacion=NOW() WHERE id_posdat=%s",
                        ("reconcile-dbf: no está en POSDAT.DBF; anulada (tiene link de compra)",
                         d["id"]), conn=conn)
                    n_anul += 1
                else:
                    db.execute("DELETE FROM scintela.posdat WHERE id_posdat=%s", (d["id"],), conn=conn)
                    n_del += 1
            for i in ins:
                db.execute(
                    "INSERT INTO scintela.posdat (prov, importe, concepto, banc, usuario_crea) "
                    "VALUES (%s, %s, %s, 0, %s)",
                    (i["prov"], i["importe"], i["concepto"], "reconcile-dbf"), conn=conn)
                n_ins += 1
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] rollback — {exc!r}")
        return

    yield line(f"APLICADO ✓ — {n_up} UPDATE, {n_del} DELETE, {n_anul} ANULADAS (linkeadas), {n_ins} INSERT.")
    nuevo = _leer_pc_banc0()
    yield line(f"PC banc=0 ahora: {len(nuevo)} filas, suma {sum(x['importe'] for x in nuevo):,.2f}")
    yield line(f"dBase: {dbf_sum:,.2f}")
