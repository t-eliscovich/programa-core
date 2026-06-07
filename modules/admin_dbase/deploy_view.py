"""Endpoint /admin/deploy — 1-click deploy del último main desde GitHub.

Patrón gemelo a /admin/migraciones y /admin/dbase-sync (mismo módulo).
Reemplaza el dance de RDP/SSM al EC2 para hacer `git pull` y reiniciar
la Scheduled Task.

Por qué hay que reiniciar (Tamara preguntó 2026-05-29):
    El Flask app corre dentro de Waitress, que carga TODO el código de
    Python en memoria una sola vez al arrancar. Cuando `git pull` cambia
    archivos en disco, el proceso vivo sigue sirviendo con la versión
    vieja en RAM. Para que aparezca el código nuevo, el proceso tiene
    que morir y volver a nacer — eso es lo que hace Restart-ScheduledTask.

Uso:
    1. Tamara/Federico → /admin/deploy (browser).
    2. Click "Deployar último commit" → corre `git pull origin main` y
       muestra el output en vivo (qué archivos cambiaron).
    3. Después dispara Restart-ScheduledTask en background — la página
       avisa "reiniciando, esperá 10s". El reload aparece con el código
       nuevo activo.

Seguridad: requiere `admin_dbase.ver` igual que los otros endpoints
de operación. NO acepta query-string para elegir branch; siempre
`origin main` (hardcoded).
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, Response, render_template, stream_with_context

from auth import requiere_login, requiere_permiso

_LOG = logging.getLogger("programa_core.admin_dbase.deploy")

bp = Blueprint(
    "admin_deploy",
    __name__,
    url_prefix="/admin/deploy",
    template_folder="templates",
)

# Paths consistentes con migraciones_view / admin_dbase.views.
if sys.platform == "win32":
    PC_ROOT = Path(r"C:\programa-core")
    GIT_EXE = "git"            # asumimos PATH; sino "C:\\Program Files\\Git\\cmd\\git.exe"
    POWERSHELL = "powershell"
else:
    PC_ROOT = Path(__file__).resolve().parent.parent.parent
    GIT_EXE = "git"
    POWERSHELL = None

SCHEDULED_TASK_NAME = "ProgramaCoreApp"


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def index():
    return render_template("admin_dbase/deploy.html")


@bp.route("/pull", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def pull():
    """Corre `git pull origin main` en C:\\programa-core y stream del output.

    NO reinicia automáticamente — la dueña aprieta "Reiniciar app" en un
    segundo paso (separamos para que pueda revisar qué cambió antes).
    """

    def _generate():
        yield "=== Sync hard a origin/main ===\n"
        yield f"cwd: {PC_ROOT}\n\n"
        if not PC_ROOT.exists():
            yield f"ERROR: no encuentro {PC_ROOT}\n"
            return
        # TMT 2026-05-29 dueña: el git pull simple se trababa con 'Your
        # local changes would be overwritten' por drift acumulado en el
        # server. Cambio a fetch + reset --hard origin/main — siempre
        # sincroniza el server al remoto, descartando cualquier diff
        # local (que no debería existir en un server de prod).
        steps = [
            (["fetch", "origin", "main"], "git fetch origin main"),
            (["reset", "--hard", "origin/main"], "git reset --hard origin/main"),
            (["log", "--oneline", "-1"], "git log -1"),
        ]
        for args, label in steps:
            yield f"--- {label} ---\n"
            try:
                proc = subprocess.Popen(
                    [GIT_EXE, *args],
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
                if proc.returncode != 0:
                    yield f"\n✗ {label} salió con exit={proc.returncode}\n"
                    return
            except FileNotFoundError:
                yield (
                    "\nERROR: no encuentro git. Instalar Git for Windows o "
                    "ajustar GIT_EXE en deploy_view.py.\n"
                )
                return
            except Exception as exc:  # noqa: BLE001
                yield f"\nERROR en {label}: {exc!r}\n"
                return
            yield "\n"
        yield "✓ Sync OK. Apretá 'Reiniciar app' para que tome.\n"

    return Response(stream_with_context(_generate()), mimetype="text/plain")


@bp.route("/restart", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def restart():
    """Reinicia la Scheduled Task. La request muere a los pocos segundos
    cuando Waitress recibe el kill — devolvemos rápido un texto plano y
    el cliente reintenta GET / a los 10s para confirmar que volvió.
    """

    def _generate():
        yield "=== Reiniciando ProgramaCoreApp ===\n"
        if sys.platform != "win32":
            yield "WARN: no estoy en Windows. Skipping restart.\n"
            yield "✓ (dev) — reiniciá el server a mano.\n"
            return
        # TMT 2026-05-29: Restart-ScheduledTask requiere el PSModule
        # ScheduledTasks, que en esta versión de Windows Server NO está
        # cargado por default ('Restart-ScheduledTask is not recognized').
        # Usamos schtasks.exe — el comando clásico que existe en TODAS las
        # versiones de Windows desde XP. Dos pasos: End (kill instancia)
        # + Run (arranca nueva). Equivalente funcional a Restart-* sin la
        # dependencia del module.
        # TMT 2026-06-07: matar el python.exe HUÉRFANO. `schtasks /End` para
        # la instancia de la tarea pero NO siempre mata el python hijo, que
        # queda tomando el puerto 5002 → la instancia nueva no puede bindear,
        # sale, y el código nuevo NUNCA se carga (deploys no-op silenciosos).
        # Matamos python/pythonw entre End y Run.
        cmd_script = (
            f"schtasks /End /TN '{SCHEDULED_TASK_NAME}'; "
            f"Start-Sleep -Seconds 2; "
            f"Get-Process python,pythonw -ErrorAction SilentlyContinue | "
            f"Stop-Process -Force -ErrorAction SilentlyContinue; "
            f"Start-Sleep -Seconds 2; "
            f"schtasks /Run /TN '{SCHEDULED_TASK_NAME}'"
        )
        yield f"PowerShell: {cmd_script}\n\n"
        try:
            # No await — disparamos y soltamos. Si esperamos, el restart
            # nos mata mientras leemos stdout y la respuesta queda colgada.
            # NB: el restart mata Waitress y por ende este mismo proceso.
            # El cliente verá la conexión cerrarse — eso es la señal de
            # que el restart efectivamente ocurrió.
            subprocess.Popen(
                [POWERSHELL, "-NoProfile", "-Command", cmd_script],
                cwd=str(PC_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                              | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            yield "Comando lanzado. La app se reinicia en pocos segundos.\n"
            yield "Esperá ~10s y refrescá la página principal.\n"
        except FileNotFoundError:
            yield "ERROR: no encuentro PowerShell.\n"
        except Exception as exc:  # noqa: BLE001
            yield f"ERROR: {exc!r}\n"

    return Response(stream_with_context(_generate()), mimetype="text/plain")
