"""Entrypoint del cron: sync del maestro de clientes Asinfo → PC.

Corre 2× por día en el EC2 (Scheduled Task "Intela-Sync-Clientes-Asinfo",
11:00 y 16:00 hora Ecuador = 16:00 y 21:00 UTC). No va por HTTP: carga el
.env (python-dotenv, igual que check_salud_dia.py), abre el pool y llama a
`modules.clientes.sync_asinfo.sincronizar()` directo — el mismo código que
el botón "Sincronizar ahora" de /clientes/sync-asinfo.

Exit code 0 si el pase corrió (aunque haya conflictos: son para personas),
1 si ni siquiera pudo correr (Metabase caído, DB caída). Imprime el reporte
en JSON para el log del Scheduled Task.

Uso manual:
    python scripts/sync_clientes_asinfo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from modules.clientes import sync_asinfo  # noqa: E402


def main() -> int:
    db.init_pool()
    reporte = sync_asinfo.sincronizar(usuario="cron-sync-clientes")
    print(json.dumps(reporte, ensure_ascii=False, default=str))
    return 0 if reporte.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
