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


# ---------------------------------------------------------------------------
# El script que lo pone en el aire
# ---------------------------------------------------------------------------

BOOTSTRAP = (ROOT / "scripts" / "crear_servicio_portal.ps1").read_text(encoding="utf-8")


def test_el_script_deriva_el_arranque_del_que_ya_funciona():
    """No escribe la línea de arranque a mano: la copia de la tarea de la
    oficina y le cambia la puerta y el puerto. Si mañana cambia la forma de
    arrancar —otra versión de Waitress, otro python— el portal la hereda."""
    assert "Get-ScheduledTask -TaskName $TAREA_BASE" in BOOTSTRAP
    assert "-replace 'run:app', 'run_portal:app'" in BOOTSTRAP


def test_el_script_frena_si_no_pudo_derivar_el_arranque():
    """🚨 Si el `-replace` no encontró nada, los argumentos quedarían IGUALES a
    los de la oficina: sería un segundo proceso del ERP escuchando en otro
    puerto, o sea el ERP entero servido donde va el portal."""
    assert "if ($argumentos -eq $accionBase.Arguments)" in BOOTSTRAP
    assert "$argumentos -notmatch 'run_portal:app'" in BOOTSTRAP


def test_el_script_no_pide_certificado_para_un_sitio_caido():
    """Let's Encrypt da pocos intentos por semana. Primero se comprueba que el
    portal contesta en localhost; recién ahí se toca el Caddyfile."""
    i_health = BOOTSTRAP.index("El portal en localhost")
    i_caddy = BOOTSTRAP.index("2. El Caddyfile")
    assert i_health < i_caddy


def test_el_script_hace_copia_del_caddyfile_y_sabe_volver():
    """El Caddyfile es el que sirve el ERP entero por HTTPS. Se toca con copia
    al lado y con vuelta atrás si Caddy rechaza la configuración nueva."""
    assert "Copy-Item $CADDYFILE $copia -Force" in BOOTSTRAP
    assert "Copy-Item $copia $CADDYFILE -Force" in BOOTSTRAP, (
        "no sabe volver atrás si Caddy rechaza la config")


def test_el_script_es_idempotente_con_el_caddyfile():
    assert "if ($texto -match [regex]::Escape($HOSTNAME_WEB))" in BOOTSTRAP


def test_el_script_no_toca_lo_de_la_oficina():
    """Agrega al FINAL del Caddyfile, nunca lo reescribe: el bloque de
    programa.intela.com.ec sigue donde estaba.

    El assert mira las líneas de CÓDIGO, no el archivo entero: el sitio de la
    oficina se nombra en el comentario de arriba justamente para decir que no
    se toca, y buscar el texto suelto daba un falso positivo por eso.
    """
    assert "Add-Content -Path $CADDYFILE" in BOOTSTRAP
    assert "Set-Content -Path $CADDYFILE" not in BOOTSTRAP
    # Fuera el bloque de ayuda de arriba (<# ... #>) y los comentarios de línea.
    cuerpo = BOOTSTRAP.split("#>", 1)[1]
    codigo = [ln for ln in cuerpo.split("\n")
              if ln.strip() and not ln.strip().startswith("#")]
    tocan_la_oficina = [ln for ln in codigo
                        if "programa.intela.com.ec" in ln.replace("portal.intela.com.ec", "")]
    assert tocan_la_oficina == [], tocan_la_oficina


def test_el_script_no_usa_here_strings():
    """🚨 El 24/08 el script entero no compiló por esto. En PowerShell el
    cierre de un here-string (`"@`) tiene que ir PEGADO al margen izquierdo, y
    adentro de un bloque indentado eso no se ve venir. Un array de líneas con
    `-join` hace lo mismo sin la trampa.

    Y no es un detalle de estilo: acá no hay forma de correr PowerShell antes
    de mandarlo al server, así que lo que no se puede probar se evita.
    """
    cuerpo = BOOTSTRAP.split("#>", 1)[1]
    # Sólo las líneas de código: el comentario de arriba nombra el here-string
    # justamente para explicar por qué no se usa.
    codigo = "\n".join(ln for ln in cuerpo.split("\n")
                        if not ln.strip().startswith("#"))
    assert '@"' not in codigo
    assert "'@" not in codigo


def test_el_script_es_ASCII_puro():
    """🚨 Windows PowerShell 5.1 lee un `.ps1` SIN BOM como Windows-1252, no
    como UTF-8. Cada carácter acentuado se decodifica mal y el parser termina
    cortando una cadena por la mitad — y el error que tira no dice "encoding",
    dice `Unexpected token` en un renglón que está perfecto.

    Pasó el 24/08 y costó dos corridas contra el server entenderlo. Acá no hay
    forma de correr PowerShell antes de mandarlo, así que esto lo cuida el
    test."""
    crudo = (ROOT / "scripts" / "crear_servicio_portal.ps1").read_bytes()
    fuera = [(i, b) for i, b in enumerate(crudo) if b > 127]
    assert not fuera, (
        f"{len(fuera)} byte(s) fuera de ASCII, el primero en la posición "
        f"{fuera[0][0]}: ...{crudo[max(0, fuera[0][0] - 40):fuera[0][0] + 10]!r}")
