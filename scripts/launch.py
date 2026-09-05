"""El lanzador del programa en el servidor — sin PowerShell.

    C:\\Python312\\python.exe C:\\programa-core\\scripts\\launch.py oficina
    C:\\Python312\\python.exe C:\\programa-core\\scripts\\launch.py portal

Fase 3 del plan docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md. Hasta el 05/09/2026
cada programa del servidor arrancaba envuelto en un `powershell.exe` (el
lanzador `launch_core.ps1`, que vive sólo en el server) que hacía tres cosas:
exportar las variables de máquina, rotar los logs y redirigir la salida a un
archivo. Cinco de esos powershell (los cuatro programas y Metabase) más sus
ocho `conhost.exe` se llevaban ~390 MB de los 4 GB de la máquina para no hacer
nada más que esperar. Esto hace lo mismo en el mismo proceso que sirve.

Lo que hace, en orden:
  1. Lee las variables de MÁQUINA del registro y las pone en el entorno (sin
     pisar las que ya vienen). El Programador de tareas a veces arranca con un
     bloque de entorno viejo (la variable nueva no está): leer el registro es
     lo que ya hace `formulas_memos._url_configurada` como fallback.
  2. Rota `logs/` (borra los de más de 14 días) y manda stdout/stderr y el
     `logging` a `logs/<nombre>-YYYY-MM-DD.log`, como hacía el .ps1.
  3. Fija el puerto (5002 oficina, 5004 portal) y el modo, importa la app
     por su puerta de siempre (`run` / `run_portal`, que ya liberan el puerto
     y cargan el `.env`) y llama a `waitress.serve` acá mismo.

Si se cae, sale con el código de la excepción y el Programador de tareas lo
vuelve a levantar (RestartCount). Sale con 3 si falta una variable obligatoria,
con una línea en el log que dice cuál.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
DIAS_DE_LOG = 14
PUERTOS = {"oficina": 5002, "portal": 5004}
#: Sin esto el programa no arranca bien; mejor una línea clara en el log que
#: un traceback de psycopg2 tres capas más abajo.
OBLIGATORIAS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def variables_de_maquina() -> dict[str, str]:
    """Las variables de entorno de MÁQUINA, leídas del registro de Windows.
    En otro sistema, vacío."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        clave = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        salida: dict[str, str] = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave) as k:
            i = 0
            while True:
                try:
                    nombre, valor, _tipo = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if isinstance(valor, str) and valor:
                    salida[nombre] = valor
        return salida
    except Exception:  # noqa: BLE001 -- sin registro, lo que haya en el entorno
        return {}


def cargar_entorno(extra: dict[str, str] | None = None) -> int:
    """Pone en `os.environ` lo que falte. Devuelve cuántas puso."""
    puestas = 0
    for nombre, valor in (extra if extra is not None else variables_de_maquina()).items():
        if nombre not in os.environ:
            os.environ[nombre] = valor
            puestas += 1
    return puestas


def rotar_logs(carpeta: Path = LOGS, dias: int = DIAS_DE_LOG, ahora: float | None = None) -> int:
    """Borra los `*.log` de más de `dias` días. Devuelve cuántos borró."""
    carpeta.mkdir(parents=True, exist_ok=True)
    ahora = time.time() if ahora is None else ahora
    borrados = 0
    for f in carpeta.glob("*.log"):
        try:
            if ahora - f.stat().st_mtime > dias * 86400:
                f.unlink()
                borrados += 1
        except OSError:
            continue
    return borrados


def archivo_de_log(nombre: str, carpeta: Path = LOGS, hoy: date | None = None) -> Path:
    hoy = hoy or date.today()
    return carpeta / f"{nombre}-{hoy.isoformat()}.log"


def redirigir_salida(ruta: Path):
    """stdout, stderr y `logging` al archivo, con línea por línea al disco."""
    f = open(ruta, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 -- vive lo que el proceso
    sys.stdout = f
    sys.stderr = f
    logging.basicConfig(stream=f, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
    return f


def faltantes(entorno=None) -> list[str]:
    entorno = os.environ if entorno is None else entorno
    return [v for v in OBLIGATORIAS if not (entorno.get(v) or "").strip()]


def preparar(nombre: str) -> int:
    """Todo lo previo a servir. Devuelve el puerto."""
    if nombre not in PUERTOS:
        raise SystemExit(f"uso: launch.py {'|'.join(PUERTOS)}")
    puerto = PUERTOS[nombre]
    cargar_entorno()
    rotar_logs()
    redirigir_salida(archivo_de_log(nombre))
    print(f"=== arranque {nombre} {datetime.now().isoformat()} pid {os.getpid()} puerto {puerto} ===")
    os.environ["PUERTO_APP"] = str(puerto)
    if nombre == "portal":
        os.environ["MODO"] = "portal"
    return puerto


def main(argv: list[str]) -> int:
    nombre = (argv[1] if len(argv) > 1 else "").strip().lower()
    puerto = preparar(nombre)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    # El .env lo carga `run` (load_dotenv), pero las obligatorias se miran
    # DESPUÉS de eso, no antes: por eso el import va primero.
    import importlib

    modulo = importlib.import_module("run_portal" if nombre == "portal" else "run")
    if faltan := faltantes():
        print(f"ERROR: faltan variables obligatorias: {', '.join(faltan)}")
        return 3
    from waitress import serve

    try:
        serve(modulo.app, host="0.0.0.0", port=puerto, ident="Intela")
    except Exception as e:  # noqa: BLE001
        print(f"=== SALIO {datetime.now().isoformat()} error: {e!r} ===")
        return 1
    print(f"=== SALIO {datetime.now().isoformat()} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
