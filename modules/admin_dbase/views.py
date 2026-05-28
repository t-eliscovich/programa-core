"""Sync dBase en 1 click — TMT 2026-05-28.

Reemplaza el dance manual de CloudShell + S3 + SSM por una sola página:
    1. Tamara entra a /admin/dbase-sync desde el browser
    2. Adjunta dbf-fresh.tar.gz (el que arma el sandbox desde /Files)
    3. El backend lo recibe, lo extrae a C:\\dbase_fresh y corre
       scripts/sync_dbase_actual.py con I_KNOW_THIS_IS_PROD=1
    4. Stream del stdout/stderr en vivo al browser

POSDAT siempre se excluye del extract (la dueña lo edita a mano por ahora).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from flask import Blueprint, Response, render_template, request, stream_with_context

from auth import requiere_login, requiere_permiso

_LOG = logging.getLogger("programa_core.admin_dbase")

bp = Blueprint(
    "admin_dbase",
    __name__,
    url_prefix="/admin/dbase-sync",
    template_folder="templates",
)

# Paths en EC2 Windows. En dev (mac/linux) caen a /tmp para no romper imports.
if sys.platform == "win32":
    TARBALL_PATH = Path(r"C:\dbase_fresh.tar.gz")
    EXTRACT_DIR = Path(r"C:\dbase_fresh")
    PYTHON_EXE = Path(r"C:\Python312\python.exe")
    PC_ROOT = Path(r"C:\programa-core")
else:
    TARBALL_PATH = Path("/tmp/dbf-fresh.tar.gz")
    EXTRACT_DIR = Path("/tmp/dbase_fresh")
    PYTHON_EXE = Path(sys.executable)
    PC_ROOT = Path(__file__).resolve().parent.parent.parent

# Archivos que nunca extraemos al destino, aunque vengan en el tarball.
# POSDAT lo edita la dueña a mano; si el sync lo pisa, se rompe el trabajo manual.
NEVER_EXTRACT = {"POSDAT.DBF"}

# Tamaño máximo aceptado del tarball (10MB es holgado — el real pesa ~900KB).
MAX_TARBALL_BYTES = 10 * 1024 * 1024


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def form():
    """Formulario para subir el tarball."""
    return render_template(
        "admin_dbase/sync.html",
        never_extract=sorted(NEVER_EXTRACT),
        target_dir=str(EXTRACT_DIR),
    )


@bp.route("/run", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def run():
    """Recibe tarball, extrae, corre sync_dbase_actual.py, streamea stdout."""
    f = request.files.get("tarball")
    if not f or not f.filename:
        return Response("ERROR: falta el archivo 'tarball'.\n", mimetype="text/plain", status=400)

    # Validamos extensión y tamaño antes de tocar disco.
    if not f.filename.lower().endswith((".tar.gz", ".tgz")):
        return Response(
            f"ERROR: extensión inesperada ({f.filename!r}). Esperaba .tar.gz / .tgz.\n",
            mimetype="text/plain",
            status=400,
        )

    # Persistimos el upload.
    TARBALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TARBALL_PATH.exists():
        TARBALL_PATH.unlink()
    f.save(TARBALL_PATH)

    size = TARBALL_PATH.stat().st_size
    if size == 0:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response("ERROR: archivo vacío.\n", mimetype="text/plain", status=400)
    if size > MAX_TARBALL_BYTES:
        TARBALL_PATH.unlink(missing_ok=True)
        return Response(
            f"ERROR: tarball pesa {size:,} bytes (max {MAX_TARBALL_BYTES:,}).\n",
            mimetype="text/plain",
            status=400,
        )

    return Response(
        stream_with_context(_run_pipeline(size, f.filename)),
        mimetype="text/plain",
    )


def _run_pipeline(tarball_bytes: int, original_name: str):
    """Generador que loguea cada paso al cliente y dispara el sync."""

    def line(msg: str) -> str:
        return msg.rstrip("\n") + "\n"

    yield line(f"[1/4] tarball recibido: {original_name} ({tarball_bytes:,} bytes)")

    # Limpiar el directorio destino antes de extraer.
    try:
        if EXTRACT_DIR.exists():
            shutil.rmtree(EXTRACT_DIR)
        EXTRACT_DIR.mkdir(parents=True)
    except OSError as exc:
        yield line(f"[ERROR] no pude limpiar {EXTRACT_DIR}: {exc}")
        return

    # Extraer, filtrando archivos vetados (POSDAT.DBF).
    extracted: list[str] = []
    skipped: list[str] = []
    try:
        with tarfile.open(TARBALL_PATH, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                # Solo el basename para evitar path traversal.
                name = Path(member.name).name
                if name.upper() in NEVER_EXTRACT:
                    skipped.append(name)
                    continue
                # Forzamos extract plano (sin subdirs).
                member.name = name
                tar.extract(member, EXTRACT_DIR)
                extracted.append(name)
    except (tarfile.TarError, OSError) as exc:
        yield line(f"[ERROR] no pude extraer el tarball: {exc}")
        return

    yield line(f"[2/4] extraídos {len(extracted)} archivos a {EXTRACT_DIR}")
    if skipped:
        yield line(f"       saltados (NEVER_EXTRACT): {', '.join(skipped)}")

    # Validar que haya al menos 1 DBF.
    dbfs = sorted(p.name for p in EXTRACT_DIR.glob("*.DBF"))
    if not dbfs:
        yield line(f"[ERROR] no se encontraron *.DBF en {EXTRACT_DIR}; abortando.")
        return
    yield line(f"       DBFs disponibles ({len(dbfs)}): {', '.join(dbfs[:6])}{' …' if len(dbfs) > 6 else ''}")

    # Disparar el sync. Usamos el mismo script que el sync manual.
    sync_script = PC_ROOT / "scripts" / "sync_dbase_actual.py"
    if not sync_script.exists():
        yield line(f"[ERROR] no existe {sync_script}")
        return

    cmd = [
        str(PYTHON_EXE),
        str(sync_script),
        "--source",
        str(EXTRACT_DIR),
    ]
    yield line(f"[3/4] corriendo: {' '.join(cmd)}")
    yield line("")

    env = os.environ.copy()
    env["I_KNOW_THIS_IS_PROD"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(  # noqa: S603 — argumentos vienen del propio servidor.
            cmd,
            cwd=str(PC_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        yield line(f"[ERROR] no pude lanzar el subprocess: {exc}")
        return

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            yield raw if raw.endswith("\n") else raw + "\n"
    finally:
        proc.stdout.close()
        rc = proc.wait()

    yield line("")
    yield line(f"[4/4] sync_dbase_actual.py terminó con exit={rc}")
    if rc == 0:
        yield line("OK ✓")
    else:
        yield line("FALLO ✗ — revisar el log de arriba.")
