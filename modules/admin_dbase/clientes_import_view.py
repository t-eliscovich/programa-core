"""Endpoint /admin/clientes-import — completa la ficha de clientes desde CLIENTES.DBF.

CLIENTES.DBF NO está en el sync normal (no tiene mapper), así que las fichas
(dirección, teléfono, RUC, provincia/cantón/parroquia, vendedor) de los clientes
agregados/editados en el dBase después de la carga inicial nunca llegaron a PC.

Política CONSERVADORA (rellenar-solo):
- Cliente que existe en PC → UPDATE SÓLO de los campos que en PC están VACÍOS y
  el DBF trae. NO pisa datos ya cargados en PC (evita meter encoding roto o
  perder ediciones hechas en PC).
- Cliente que falta en PC → INSERT.
- El DBF se DEDUPLICA por código (tiene ~25 repetidos); se prefiere la fila con
  dirección cargada.
- Encoding cp850 (igual que import_dbf) para que ñ/acentos salgan bien.

Dry-run por defecto. "Aplicar" lo ejecuta en una transacción. Mapeo DBF→cliente:
  CLIENTE→codigo_cli, NOMBRE→nombre, TELEFONO→telefono, RUC→ruc,
  DIRECCION→direccion1, DIRECCION1→direccion2,
  PROV→provincia, CANTON→canton, PARROQ→parroquia, VEND→vend.
TMT 2026-06-06.
"""
from __future__ import annotations

import shutil
import sys
import tarfile
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("clientes_import", __name__, url_prefix="/admin/clientes-import")

if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\clientes_import.tar.gz")
    EXTRACT_DIR = Path(r"C:\clientes_import")
else:
    TARBALL_PATH = Path("/tmp/clientes_import.tar.gz")
    EXTRACT_DIR = Path("/tmp/clientes_import")

MAX_TARBALL_BYTES = 10 * 1024 * 1024

# (col_pc, campo_dbf, maxlen)
FICHA = [
    ("nombre", "NOMBRE", 200),
    ("telefono", "TELEFONO", 30),
    ("ruc", "RUC", 16),
    ("direccion1", "DIRECCION", 200),
    ("direccion2", "DIRECCION1", 200),
    ("provincia", "PROV", 60),
    ("canton", "CANTON", 60),
    ("parroquia", "PARROQ", 60),
    ("vend", "VEND", 50),
]


def _s(v, maxlen=None) -> str:
    s = ("" if v is None else str(v)).strip()
    return s[:maxlen] if maxlen else s


def _leer_dbf(dbf_path: Path) -> list[dict]:
    """Lee CLIENTES.DBF (cp850) y deduplica por código (prefiere fila con dir)."""
    import dbfread
    by_code: dict[str, dict] = {}
    for r in dbfread.DBF(str(dbf_path), encoding="cp850",
                         char_decode_errors="replace", load=False):
        cod = _s(r.get("CLIENTE"), 5).upper()
        if not cod:
            continue
        row = {"codigo_cli": cod, **{col: _s(r.get(dbf), ml) for col, dbf, ml in FICHA}}
        prev = by_code.get(cod)
        if (prev is None
                or (not (prev["direccion1"] or prev["direccion2"])
                    and (row["direccion1"] or row["direccion2"]))):
            by_code[cod] = row
    return list(by_code.values())


def _leer_pc() -> dict:
    import db
    rows = db.fetch_all(
        "SELECT codigo_cli, nombre, telefono, ruc, direccion1, direccion2, "
        "provincia, canton, parroquia, vend FROM scintela.cliente"
    ) or []
    return {(_s(r["codigo_cli"]).upper()): r for r in rows if _s(r.get("codigo_cli"))}


FORM = """
<!doctype html><meta charset=utf-8><title>Importar fichas de clientes</title>
<div style="max-width:640px;margin:2rem auto;font-family:system-ui">
<h2>Importar fichas de clientes desde CLIENTES.DBF</h2>
<p>Subí el tarball con CLIENTES.DBF (el mismo de /admin/dbase-sync). Rellena
SÓLO los campos vacíos (dirección/teléfono/RUC/provincia) y agrega los clientes
que falten. No pisa datos ya cargados. Corre en <b>DRY-RUN</b> salvo que marques
Aplicar.</p>
<form method=post action="/admin/clientes-import/run" enctype="multipart/form-data">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=file name=tarball accept=".tar.gz,.tgz,application/gzip,application/x-gzip,application/x-tar" required><br><br>
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

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== Importar fichas clientes — {'APLICAR' if aplicar else 'DRY-RUN'} ===")

    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
        dbf_path = None
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and Path(m.name).name.upper() == "CLIENTES.DBF":
                    m.name = "CLIENTES.DBF"
                    tar.extract(m, EXTRACT_DIR)
                    dbf_path = EXTRACT_DIR / "CLIENTES.DBF"
                    break
        if not dbf_path or not dbf_path.exists():
            yield line("[ERROR] el tarball no contiene CLIENTES.DBF")
            return
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] no pude extraer: {exc!r}")
        return

    yield from importar_desde_dbf(dbf_path, aplicar)


def importar_desde_dbf(dbf_path: Path, aplicar: bool, verbose: bool = True):
    """Core del import — generator de líneas de log.

    Reutilizado por el endpoint standalone (/admin/clientes-import) y por el
    hook post-sync de /admin/dbase-sync (TMT 2026-06-10: las altas nuevas de
    clientes en el dBase no llegaban a PC porque CLIENTES.DBF no tiene mapper
    en el sync — facturas de Asinfo rebotaban con "cliente no existe").

    `verbose=False` omite los listados por-cliente (para no inflar el log del
    sync); el PLAN y el resultado APLICADO se loguean siempre.
    """
    import db

    def line(m=""):
        return m.rstrip("\n") + "\n"

    dbf = _leer_dbf(dbf_path)
    pc = _leer_pc()
    dbf_codes = {d["codigo_cli"] for d in dbf}
    pc_only = [c for c in pc if c not in dbf_codes]

    yield line(f"DBF: {len(dbf)} clientes únicos  |  PC: {len(pc)} clientes")
    yield line(f"  PC que NO están en el DBF (quedan intactos, no se borran): {len(pc_only)}")
    if verbose:
        for c in sorted(pc_only):
            cur = pc[c]
            yield line(f"      solo-PC: [{c}] {_s(cur.get('nombre'))[:45]}  "
                       f"tel={_s(cur.get('telefono'))[:14]} ruc={_s(cur.get('ruc'))[:14]}")
    yield line("")

    inserts = []
    updates = []  # (codigo, {col: val})
    dir_nuevas = 0
    for d in dbf:
        cod = d["codigo_cli"]
        if cod not in pc:
            inserts.append(d)
            if d["direccion1"] or d["direccion2"]:
                dir_nuevas += 1
            continue
        cur = pc[cod]
        cambios = {}
        for col, _dbf, _ml in FICHA:
            val = d[col]
            if val and not _s(cur.get(col)):  # RELLENAR-SOLO (PC vacío)
                cambios[col] = val
        if cambios:
            if "direccion1" in cambios or "direccion2" in cambios:
                dir_nuevas += 1
            updates.append((cod, cambios))

    yield line(f"PLAN: {len(updates)} UPDATE (rellenan vacíos) · {len(inserts)} INSERT · "
               f"{dir_nuevas} clientes que GANAN dirección")
    yield line(f"Después PC tendría: {len(pc) + len(inserts)} clientes "
               f"({len(dbf_codes)} del DBF + {len(pc_only)} solo-PC)")
    yield line("")
    if verbose:
        yield line("--- INSERT (faltan en PC) ---")
        for d in inserts[:120]:
            yield line(f"  {d['codigo_cli']:6} {d['nombre'][:34]:34} {d['direccion1'][:28]}")
        if len(inserts) > 120:
            yield line(f"  … +{len(inserts) - 120} más")
        yield line("")
        yield line("--- UPDATE (rellenan campos vacíos) — primeros 80 ---")
        for cod, cambios in updates[:80]:
            yield line(f"  {cod:6} {', '.join(cambios)}")
        if len(updates) > 80:
            yield line(f"  … +{len(updates) - 80} más")
        yield line("")
    elif inserts:
        yield line("  nuevos: " + ", ".join(d["codigo_cli"] for d in inserts[:40])
                   + (f" … +{len(inserts)-40}" if len(inserts) > 40 else ""))

    if not aplicar:
        yield line("DRY-RUN: no se tocó nada. Marcá 'Aplicar' para ejecutar.")
        return

    n_up = n_ins = 0
    try:
        with db.tx() as conn:
            for cod, cambios in updates:
                sets = ", ".join(f"{c} = %s" for c in cambios)
                params = list(cambios.values()) + ["clientes-import", cod]
                db.execute(
                    f"UPDATE scintela.cliente SET {sets}, usuario_modifica = %s "
                    "WHERE UPPER(codigo_cli) = %s",
                    tuple(params), conn=conn)
                n_up += 1
            for d in inserts:
                db.execute(
                    "INSERT INTO scintela.cliente "
                    "(codigo_cli, nombre, telefono, ruc, direccion1, direccion2, "
                    " provincia, canton, parroquia, vend, stop, usuario_crea) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'N',%s)",
                    (d["codigo_cli"], d["nombre"] or d["codigo_cli"], d["telefono"] or None,
                     d["ruc"] or None, d["direccion1"] or None, d["direccion2"] or None,
                     d["provincia"] or None, d["canton"] or None, d["parroquia"] or None,
                     d["vend"] or None, "clientes-import"), conn=conn)
                n_ins += 1
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] rollback — {exc!r}")
        return

    yield line(f"APLICADO ✓ — {n_up} UPDATE, {n_ins} INSERT, {dir_nuevas} con dirección nueva.")
