"""Corrida MANUAL del sync de clientes Asinfo → PC (para debug en el server).

⚠ NO hay Scheduled Task del EC2 para esto (dueña 05/08/2026: "no hacemos
eso" — nada de este ciclo va por cron del EC2). Las corridas automáticas (cada
hora de 07:00 a 19:00 EC) las hace el hilo de fondo de la app
(`modules/_lib/autocarga_facturas.py` → `sync_asinfo.correr_si_toca()`).

Este script queda como entrypoint manual: carga el .env (python-dotenv,
igual que check_salud_dia.py), abre el pool y llama a `sincronizar()` —
el mismo código que el botón "Sincronizar ahora" de /clientes/sync-asinfo.

Exit code 0 si el pase corrió (aunque haya conflictos: son para personas),
1 si ni siquiera pudo correr (Metabase caído, DB caída). Imprime el reporte
en JSON.

Uso:
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
