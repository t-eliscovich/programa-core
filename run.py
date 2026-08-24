"""Puerta de entrada del programa de la oficina.

En desarrollo:
    python run.py

En producción, en el Windows Server del EC2:
    waitress-serve --listen=0.0.0.0:5002 run:app

El portal del cliente tiene su propia puerta, `run_portal.py`, que levanta el
MISMO código en otro proceso y otro puerto. Ver `modo.py`.
"""
import os

from dotenv import load_dotenv

load_dotenv()


#: El puerto que este proceso va a tomar. Sólo se usa para matar al huérfano
#: que lo tenga agarrado; quien manda de verdad es el `--listen` de Waitress.
#: `run_portal.py` lo pisa con el suyo antes de importar esto.
PUERTO = int(os.environ.get("PUERTO_APP") or 5002)


def _liberar_puerto_si_prod(puerto: int = 0) -> None:
    """Mata al proceso HUÉRFANO que haya quedado tomando el puerto ANTES de
    que Waitress intente bindear.

    Por qué: el deploy reinicia la app con Stop/Start-ScheduledTask + kill python
    POR NOMBRE, pero a veces un python huérfano sobrevive y sigue tomando el 5002.
    Entonces la instancia NUEVA (con el código nuevo) no puede bindear, sale, y la
    VIEJA sigue sirviendo → el deploy queda "verde" (healthcheck 200 contra la
    vieja) pero el código nuevo NUNCA se carga (no-op silencioso). Verificado
    2026-07-30: la columna del flujo no cambió tras varios deploys verdes.

    Como `waitress-serve ... run:app` IMPORTA este módulo antes de bindear, matar
    acá al dueño del puerto hace que la instancia nueva SIEMPRE se imponga, sin
    depender de que el workflow lo logre. Solo corre en producción y en Windows;
    apunta únicamente a SU puerto — nunca al de otro programa (5001 formulas,
    3000 metabase, 5003 máquinas). Fail-soft absoluto: cualquier error se traga,
    nunca bloquea el arranque.
    """
    puerto = int(puerto or PUERTO)
    marca = f":{puerto}"
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "development").lower()
    if env != "production" or os.name != "nt":
        return
    try:
        import subprocess

        salida = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
        propio = str(os.getpid())
        pids: set[str] = set()
        for linea in salida.splitlines():
            if "LISTENING" not in linea or marca not in linea:
                continue
            # ...solo el LOCAL address (2ª columna) puede terminar en el puerto.
            cols = linea.split()
            if len(cols) < 5 or not cols[1].endswith(marca):
                continue
            pid = cols[-1]
            if pid.isdigit() and pid != propio and pid != "0":
                pids.add(pid)
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, text=True, timeout=10,
                )
                print(f"[run] puerto {puerto} liberado: matado PID huérfano {pid}",
                      flush=True)
            except Exception:  # noqa: BLE001 -- fail-soft
                pass
    except Exception:  # noqa: BLE001 -- nunca bloquear el arranque por esto
        pass


_liberar_puerto_si_prod()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
