"""Lo que hace que el deploy sea corto y que no se pise con otro.

TMT 2026-08-27 (dueña): *"el deploy to EC2 está muy lento, fijate por qué y
cómo bajamos tiempo"*. Medido sobre el commit f681ef5 (push → producción,
2 m 38 s): el deploy en sí eran 48 s y el CI 1 m 48 s. Lo que se recortó acá no
es el pytest —eso es otra pelea— sino la plomería alrededor, y de paso salieron
dos bugs que no eran de velocidad:

  · **dos deploys a la vez** contra el mismo server (runs 2940 y 2941 del
    27/08). El tarball se subía SIEMPRE a la misma key de S3, así que el deploy
    A podía bajarse el que subió B y dejar en producción un commit que no era
    el suyo;
  · **los backticks de los comentarios se ejecutaban**. El heredoc que arma el
    PowerShell iba sin comillas, así que bash expandía el cuerpo antes de
    mandarlo: cada `palabra` entre backticks de un comentario corría como
    comando en el runner y desaparecía del script. En el log real del 27/08
    están las víctimas ("Select: command not found", "migrations/: Is a
    directory", "unexpected EOF while looking for matching '").

Estos tests son el candado de las dos cosas.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
DEPLOY_YML = yaml.safe_load(DEPLOY)
CI_YML = yaml.safe_load(CI)


def _cuerpo_del_heredoc() -> str:
    """El PowerShell tal cual está escrito en el YAML, sin el bash de alrededor."""
    return DEPLOY[DEPLOY.index("PSEOF'"):DEPLOY.index("\n          PSEOF\n")]


# ---------------------------------------------------------------------------
# El heredoc: bash no toca el PowerShell
# ---------------------------------------------------------------------------


def test_el_heredoc_va_entre_comillas():
    """🚨 Sin las comillas del delimitador, bash EJECUTA lo que haya entre
    backticks — comentarios incluidos — y lo borra del script que llega al
    server. Un comentario nunca puede ser código."""
    assert "SCRIPT=$(cat <<'PSEOF'" in DEPLOY, (
        "el heredoc volvió a quedar sin comillas: los backticks de los "
        "comentarios se van a ejecutar en el runner")
    assert "SCRIPT=$(cat <<PSEOF" not in DEPLOY


def test_la_url_y_el_sha_entran_por_marcador():
    """Con el heredoc literal ya no se puede interpolar adentro, así que van
    como marcadores y se reemplazan afuera. Si alguien vuelve a poner
    `${DOWNLOAD_URL}` adentro, el server recibe el texto crudo y se baja nada."""
    cuerpo = _cuerpo_del_heredoc()
    assert "$url = '__DOWNLOAD_URL__'" in cuerpo
    assert "$sha = '__COMMIT_SHA__'" in cuerpo
    assert "${DOWNLOAD_URL}" not in cuerpo
    assert "${COMMIT_SHA}" not in cuerpo
    assert "SCRIPT=${SCRIPT//__DOWNLOAD_URL__/$DOWNLOAD_URL}" in DEPLOY
    assert "SCRIPT=${SCRIPT//__COMMIT_SHA__/$COMMIT_SHA}" in DEPLOY


def test_el_powershell_no_lleva_dolares_escapados():
    r"""Los `\$` eran del heredoc sin comillas. Ahora sobran, y si alguien
    copia uno el server recibe `\$var` literal — que en PowerShell es un
    dólar suelto seguido del nombre, no la variable."""
    assert "\\$" not in _cuerpo_del_heredoc()


# ---------------------------------------------------------------------------
# Dos deploys no se pisan
# ---------------------------------------------------------------------------


def test_el_tarball_va_a_una_key_por_commit():
    """🚨 La carrera de verdad: con una key fija, dos deploys solapados se
    pisan el tarball y uno deploya el commit del otro."""
    assert 'S3_KEY="programa_core_deploy/${DEPLOY_SHA}.tar.gz"' in DEPLOY
    assert 'S3_KEY="programa_core_deploy/programa_core.tar.gz"' not in DEPLOY


def test_los_deploys_se_serializan_sin_cortar_el_que_esta_corriendo():
    """Cortar un deploy a la mitad deja el server con el tar a medio extraer y
    la app parada: `cancel-in-progress` tiene que ser False."""
    conc = DEPLOY_YML["concurrency"]
    assert conc["group"] == "deploy-ec2"
    assert conc["cancel-in-progress"] is False


def test_el_ci_viejo_se_cancela_cuando_llega_otro_push():
    """Tres pushes seguidos daban tres CI y tres deploys, y el server se
    reiniciaba tres veces. El de la punta ya contiene a los otros dos."""
    conc = CI_YML["concurrency"]
    assert conc["cancel-in-progress"] is True
    assert "github.ref" in conc["group"]


def test_un_ci_cancelado_no_avisa_que_el_deploy_quedo_bloqueado():
    """🚨 El job `bloqueado` existe para que un CI ROJO se vea. Un CI cancelado
    por el `concurrency` de arriba no es un rojo — su commit viaja adentro del
    CI de la punta — y con `!= 'success'` cada push doble mandaba un mail."""
    cond = DEPLOY_YML["jobs"]["bloqueado"]["if"]
    assert "workflow_run.conclusion == 'failure'" in cond
    assert "!= 'success'" not in cond


# ---------------------------------------------------------------------------
# Nada de dormir a ciegas
# ---------------------------------------------------------------------------


def test_no_se_para_dos_veces_lo_que_ya_esta_parado():
    """Desde el 25/08 los procesos se paran ANTES de extraer (paso 3). El
    segundo `Stop-ScheduledTask` del paso 5 paraba lo que ya estaba parado y
    dormía 2 s por deploy. El kill por PUERTO sí se queda: es el que agarra al
    huérfano, y sale en 0 s cuando no hay ninguno."""
    cuerpo = _cuerpo_del_heredoc()
    assert cuerpo.count("Stop-ScheduledTask -TaskName ProgramaCoreApp") == 1
    assert cuerpo.count("Stop-ScheduledTask -TaskName PortalClienteApp") == 1
    assert "LocalPort 5002 -State Listen" in cuerpo


def test_los_health_check_preguntan_antes_de_dormir():
    """`Start-Sleep 1` arriba del loop le cobra un segundo de peaje a una app
    que ya estaba contestando — que es el caso normal desde que los dos
    procesos arrancan juntos."""
    for url in ("http://localhost:5002/login", "http://localhost:5004/"):
        i = DEPLOY.index(url)
        loop = DEPLOY[DEPLOY.rindex("for (", 0, i):i]
        assert "Start-Sleep" not in loop, (
            f"el loop de {url} volvió a dormir antes de preguntar")


def test_el_deploy_deja_los_tiempos_en_el_log():
    """Sin el reloj, la próxima vez que haya que recortar hay que adivinar si
    los segundos se van en el tar, en migrate.py o esperando al portal."""
    cuerpo = _cuerpo_del_heredoc()
    assert "$t0 = Get-Date" in cuerpo
    assert "function paso($m)" in cuerpo
    for hito in ("bajado el tarball", "extraido", "migraciones al dia",
                 "oficina: HTTP", "=== Deploy OK ==="):
        assert f'paso "{hito}' in cuerpo, f"falta el tiempo de: {hito}"
