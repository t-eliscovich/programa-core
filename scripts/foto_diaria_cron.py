"""Cron diario de la FOTO DIARIA del balance (Scheduled Task en el EC2).

Hasta el 2026-09-02 la ÚNICA forma de que `scintela.historia` tuviera una
fila `snapshot-diario` de un día era que alguien VISITARA a mano
`/admin/health/all` o `/admin/health/snapshot-diario` ese mismo día -- las
dos rutas piden login (`@requiere_login` + `usuarios.admin`), así que un
cron por HTTP hubiera rebotado con 302/401 en vez de tomar la foto. Este
script no pega la ruta: llama directo a `ejecutar_foto_diaria()` -- el
mismo código que corre la pantalla -- en proceso, sin sesión ni servidor.
Mismo patrón que ya usa `scripts/procesa_provisiones_mensual.py` para el
cierre mensual (que también invoca funciones de `modules.informes.queries`
directamente en vez de pedirle nada a Flask).

Hallazgo 2026-09-01 (ver memoria
project_2026_09_01_cierre_agosto_roto_y_hardening): en los 31 días de
agosto 2026 no se tomó NINGUNA foto diaria -- nadie visitó esa pantalla ni
una sola vez. Mientras esto no corriera solo, cualquier cierre de mes que
no coincidiera con una visita manual esa misma noche caía en la
reconstrucción aproximada (`balance_components_as_of`) en vez de la foto
real -- exactamente lo que le pasó a agosto. Este cron es el blindaje.

Uso desde el Scheduled Task de Windows (ver skill intela-aws-deploy,
sección "FormulasApp Scheduled Task" para el patrón de registro -- éste
usa el mismo `C:\\programa-core\\.venv\\Scripts\\python.exe`):

    python scripts/foto_diaria_cron.py

Exit 0 si la foto salió sin alertas. Exit 1 si `ejecutar_foto_diaria()`
devolvió `ok=False` (patrimonio saltó, stock en 0, etc. -- revisar
`/admin/health/cron-status` o `/admin/health/all`) para que el propio
Scheduled Task / un monitor externo pueda marcar la corrida en rojo sin
que nadie tenga que mirar a mano todos los días.
"""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("foto_diaria_cron")


def main() -> int:
    from modules.admin_dbase.health_audit_view import ejecutar_foto_diaria

    resultado = ejecutar_foto_diaria()
    snap = (resultado.get("stats") or {}).get("hoy") or {}
    log.info(
        "foto diaria fecha=%s patrimonio=%s ustock=%s ok=%s",
        snap.get("fecha"), snap.get("patrimonio"), snap.get("ustock"),
        resultado.get("ok"),
    )
    for a in resultado.get("alerts") or []:
        log.warning("ALERTA: %s", a)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
