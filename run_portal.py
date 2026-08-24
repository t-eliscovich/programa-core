"""Puerta de entrada del PORTAL DEL CLIENTE.

En producción, en el Windows Server del EC2:
    waitress-serve --listen=0.0.0.0:5004 run_portal:app

Es el MISMO código que el programa de la oficina, en otro proceso: mismo repo,
misma base, mismo deploy. Lo único que cambia es que acá **las pantallas del
ERP no se registran** — no existen. Ver `modo.py`.

⭐ El modo se prende ACÁ, en la puerta, y no en la configuración del servicio.
Es a propósito: si dependiera de una variable que alguien tiene que acordarse
de setear en el Programador de tareas de Windows, el día que se cree de nuevo
sin ella el portal arrancaría sirviendo el ERP entero a internet. La puerta que
elegís es el modo que corre.

Puertos que ya están tomados en ese servidor: 3000 Metabase, 5001 formulas_app,
5002 el programa de la oficina, 5003 máquinas, 8080 el proxy de kilos.
"""
import os

os.environ["MODO"] = "portal"
os.environ.setdefault("PUERTO_APP", "5004")

from run import app  # noqa: E402,F401  -- reusa TODO el arranque de siempre

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5004, debug=True)
