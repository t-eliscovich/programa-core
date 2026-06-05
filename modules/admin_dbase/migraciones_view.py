"""Endpoint /admin/migraciones — aplica migraciones pendientes en 1 click.

Patrón gemelo a /admin/dbase-sync (mismo módulo). Reemplaza el dance de
RDP al EC2 + correr `python scripts\\migrate.py` a mano.

Uso:
    1. Tamara/Federico entran a /admin/migraciones desde el browser.
    2. Ven la lista de migraciones pendientes (las que están en disco
       pero no en seguridad.migraciones_aplicadas).
    3. Click "Aplicar pendientes" → corre scripts/migrate.py y stream
       de stdout/stderr en vivo.
    4. scripts/migrate.py es idempotente — un click extra es seguro.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, Response, render_template, stream_with_context

from auth import requiere_login, requiere_permiso

_LOG = logging.getLogger("programa_core.admin_dbase.migraciones")

bp = Blueprint(
    "admin_migraciones",
    __name__,
    url_prefix="/admin/migraciones",
    template_folder="templates",
)

# Mismos paths que dbase-sync.
if sys.platform == "win32":
    PYTHON_EXE = Path(r"C:\Python312\python.exe")
    PC_ROOT = Path(r"C:\programa-core")
else:
    PYTHON_EXE = Path(sys.executable)
    PC_ROOT = Path(__file__).resolve().parent.parent.parent

MIGRATE_SCRIPT = PC_ROOT / "scripts" / "migrate.py"


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def index():
    """Página con botón 'Aplicar pendientes'. Muestra primero el status."""
    return render_template("admin_dbase/migraciones.html")


@bp.route("/run", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    """Corre scripts/migrate.py y stream del output en vivo."""

    def _generate():
        yield "=== Aplicando migraciones pendientes ===\n"
        yield f"Script: {MIGRATE_SCRIPT}\n"
        yield f"Python: {PYTHON_EXE}\n\n"
        if not MIGRATE_SCRIPT.exists():
            yield f"ERROR: no encuentro {MIGRATE_SCRIPT}\n"
            return
        try:
            proc = subprocess.Popen(
                [str(PYTHON_EXE), str(MIGRATE_SCRIPT)],
                cwd=str(PC_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line
            proc.wait()
            yield f"\n=== exit code: {proc.returncode} ===\n"
            if proc.returncode == 0:
                yield "✓ Migraciones aplicadas (o nada pendiente).\n"
            else:
                yield "✗ Hubo errores — revisá el output arriba.\n"
        except Exception as exc:  # noqa: BLE001
            yield f"\nERROR ejecutando migrate.py: {exc!r}\n"

    return Response(stream_with_context(_generate()), mimetype="text/plain")


@bp.route("/status", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def status():
    """JSON con migraciones pendientes/aplicadas. Sin correr nada."""
    import json

    import db

    aplicadas: list[str] = []
    try:
        rows = db.fetch_all(
            "SELECT version FROM seguridad.migraciones_aplicadas ORDER BY version"
        ) or []
        aplicadas = [r["version"] for r in rows]
    except Exception as exc:  # noqa: BLE001
        return Response(
            json.dumps({"error": f"no pude leer tracker: {exc!r}"}),
            status=500,
            mimetype="application/json",
        )
    migr_dir = PC_ROOT / "migrations"
    en_disco = sorted([
        f.name for f in migr_dir.iterdir()
        if f.suffix in (".sql", ".py") and f.name[:4].isdigit()
    ]) if migr_dir.exists() else []
    pendientes = [n for n in en_disco if n.rsplit(".", 1)[0] not in aplicadas
                  and n not in aplicadas]
    return Response(
        json.dumps({
            "aplicadas": aplicadas,
            "pendientes": pendientes,
            "total_en_disco": len(en_disco),
        }, indent=2),
        mimetype="application/json",
    )
