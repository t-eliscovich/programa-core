"""La puerta del portal y su lugar en el deploy.

TMT 2026-08-24. El portal del cliente es el MISMO código en otro proceso: misma
carpeta en el server, mismo deploy, otro puerto. De ahí salen las dos cosas que
se testean acá.

⭐ **El modo se prende en la PUERTA, no en la configuración del servicio.** Si
dependiera de una variable que alguien tiene que acordarse de poner en el
Programador de tareas de Windows, el día que la tarea se recree sin ella el
portal arrancaría sirviendo el ERP entero a internet.

⭐ **El deploy tiene que reiniciar las DOS tareas.** Comparten carpeta: un
deploy le cambia los archivos abajo de los pies al portal, que si no queda
sirviendo el código viejo para siempre.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FUENTE_PORTAL = (ROOT / "run_portal.py").read_text(encoding="utf-8")
FUENTE_RUN = (ROOT / "run.py").read_text(encoding="utf-8")
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# La puerta
# ---------------------------------------------------------------------------


def test_la_puerta_prende_el_modo_antes_de_armar_la_app():
    """El orden es lo único que hace que esto funcione: `create_app()` corre en
    el import de `run`, así que si el MODO se seteara después ya sería tarde."""
    i_modo = FUENTE_PORTAL.index('os.environ["MODO"] = "portal"')
    i_import = FUENTE_PORTAL.index("from run import app")
    assert i_modo < i_import, (
        "el MODO se setea DESPUÉS de importar run: la app ya se armó como ERP")


def test_la_puerta_del_portal_no_depende_del_servicio():
    """No `os.environ.get`, no `setdefault`: lo IMPONE. Ver el docstring."""
    assert 'os.environ["MODO"] = "portal"' in FUENTE_PORTAL
    assert 'setdefault("MODO"' not in FUENTE_PORTAL


def test_cada_puerta_mata_al_huerfano_de_SU_puerto():
    """🚨 El matador de huérfanos apunta al puerto propio. Si el portal matara
    al del 5002 se llevaría puesto el programa de la oficina en cada arranque
    —y si mirara un puerto fijo, no serviría para el portal."""
    assert 'os.environ.get("PUERTO_APP") or 5002' in FUENTE_RUN
    assert 'setdefault("PUERTO_APP", "5004")' in FUENTE_PORTAL
    # Y el barrido usa la variable, no un número escrito en el medio del for.
    cuerpo = FUENTE_RUN[FUENTE_RUN.index("def _liberar_puerto_si_prod"):]
    cuerpo = cuerpo[:cuerpo.index("\n_liberar_puerto_si_prod()")]
    assert 'marca = f":{puerto}"' in cuerpo
    assert ':5002"' not in cuerpo, "quedó el puerto escrito a mano en el barrido"


def test_los_puertos_no_chocan_con_los_vecinos():
    """En ese server ya viven Metabase (3000), formulas_app (5001), el programa
    de la oficina (5002), máquinas (5003) y el proxy de kilos (8080)."""
    ocupados = {"3000", "5001", "5002", "5003", "8080"}
    assert "5004" not in ocupados
    assert '"PUERTO_APP", "5004"' in FUENTE_PORTAL


# ---------------------------------------------------------------------------
# El deploy
# ---------------------------------------------------------------------------


def test_el_deploy_reinicia_tambien_el_portal():
    assert "PortalClienteApp" in DEPLOY, (
        "el deploy no toca el portal: comparte carpeta con la oficina, así que "
        "quedaría sirviendo el código viejo para siempre")
    assert "Start-ScheduledTask -TaskName PortalClienteApp" in DEPLOY


def test_el_portal_se_reinicia_DESPUES_de_la_oficina():
    """El programa con el que trabaja la oficina todo el día arranca primero."""
    i_oficina = DEPLOY.index("Start-ScheduledTask -TaskName ProgramaCoreApp")
    i_portal = DEPLOY.index("Start-ScheduledTask -TaskName PortalClienteApp")
    assert i_oficina < i_portal


def test_el_portal_caido_no_frena_el_deploy_de_la_oficina():
    """🚨 Fail-soft a propósito. Un `exit 1` acá dejaría a la oficina sin
    deployar por culpa de una pantalla que todavía no usa nadie."""
    bloque = DEPLOY[DEPLOY.index("5.b El PORTAL"):DEPLOY.index("# 6. Health check")]
    assert "exit 1" not in bloque
    assert "ErrorAction SilentlyContinue" in bloque
    assert re.search(r"if \(Get-ScheduledTask -TaskName PortalClienteApp", bloque), (
        "sin el chequeo de que la tarea existe, el deploy escupe un error "
        "rojo todos los días hasta que alguien la cree en el server")


def test_el_portal_no_toca_el_puerto_de_la_oficina():
    bloque = DEPLOY[DEPLOY.index("5.b El PORTAL"):DEPLOY.index("# 6. Health check")]
    assert "LocalPort 5004" in bloque
    assert "5002" not in bloque, "el bloque del portal está mirando el puerto de la oficina"
