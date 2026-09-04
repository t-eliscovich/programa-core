"""Arma la COPIA DE DATOS (el mismo zip de /admin/copia-de-datos) en un
archivo, sin sesión ni servidor. La corre el workflow nocturno
`.github/workflows/copia_datos.yml` por SSM en el EC2 y después sube el zip a
S3 (`copias-datos/`, 30 días). TMT 2026-09-03: *"quiero automatizar"*.

    python scripts/copia_datos_cron.py C:\\tmp\\copia.zip

Exit 0 si el zip quedó escrito; 1 si algo falló (el workflow lo marca rojo).
"""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copia_datos_cron")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log.error("uso: copia_datos_cron.py <archivo.zip>")
        return 1
    from modules.admin_dbase.copia_datos_view import armar_copia

    destino = argv[1]
    try:
        with open(destino, "wb") as f:
            info = armar_copia(f)
    except Exception as e:  # noqa: BLE001
        log.error("la copia falló: %s", e)
        return 1
    peso = os.path.getsize(destino)
    log.info("copia de datos: %s tablas, %.1f MB, hasta la migración %s -> %s",
             info["tablas"], peso / 1e6, info["migracion"], destino)
    return 0 if info["tablas"] and peso > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
