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

# ⚠ Sin `yaml`: PyYAML NO está en requirements.txt y nadie lo arrastra, así que
# en el CI no se instala y el `import` rompía la corrida entera en la
# recolección (CI #2513, 27/08). Traerlo sólo para leer un workflow sería meter
# una dependencia en PRODUCCIÓN por un test — y encima dispararía el pip
# install del próximo deploy. Los otros tests de deploy de este repo leen el
# YAML como texto; este hace lo mismo.
ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _sin_comentarios(texto: str) -> str:
    """Los comentarios de estos workflows EXPLICAN lo que ya no va (el
    `workflow_run` viejo, el service postgres que se mudó), así que nombran las
    palabras que varios asserts prohíben. Sin esto, cada test se dispara con su
    propia explicación."""
    return "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith("#"))


def _job(texto: str, nombre: str, hasta: str) -> str:
    return _sin_comentarios(texto[texto.index(nombre):texto.index(hasta)])


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
    assert ("\nconcurrency:\n"
            "  group: deploy-ec2\n"
            "  cancel-in-progress: false\n") in DEPLOY


def test_el_ci_viejo_se_cancela_cuando_llega_otro_push():
    """Tres pushes seguidos daban tres CI y tres deploys, y el server se
    reiniciaba tres veces. El de la punta ya contiene a los otros dos."""
    assert ("\nconcurrency:\n"
            "  group: ci-${{ github.ref }}\n"
            "  cancel-in-progress: true\n") in CI


def test_sin_tests_verdes_no_se_deploya():
    """🚨 EL FRENO. Antes vivía en un `if` de un workflow aparte colgado de
    `workflow_run`, y hacía falta un job `bloqueado` que fallara a propósito
    porque un deploy salteado quedaba en gris, sin mail — o sea, igual a que no
    pase nada. Ahora el deploy es un job de ci.yml que DEPENDE de los dos jobs
    de tests: si alguno se pone rojo, el deploy ni existe, y el que queda rojo
    y manda mail es el CI mismo."""
    assert "needs: [test, test-db, paquete]" in CI
    assert "uses: ./.github/workflows/deploy.yml" in CI[CI.index("  deploy:"):]
    # Y no quedó ningún camino que deploye sin pasar por ahí.
    deploy = _sin_comentarios(DEPLOY)
    assert "workflow_run" not in deploy, (
        "volvió el disparador viejo: un deploy colgado del CI TERMINADO "
        "vuelve a costar los ~9 s del salto y trae de nuevo la trampa del head_sha")
    assert "bloqueado" not in deploy


def test_el_deploy_solo_sale_de_un_push_a_main():
    """En un PR el CI corre entero, pero no puede tocar producción."""
    for job, hasta in (("  paquete:", "  deploy:"), ("  deploy:", "\n    with:")):
        assert ("if: github.event_name == 'push' && "
                "github.ref == 'refs/heads/main'") in _job(CI, job, hasta), (
            f"{job.strip()} no está limitado a un push a main: un PR tocaría producción")


def test_el_commit_a_deployar_entra_por_parametro():
    """La trampa vieja: en un `workflow_run`, `github.sha` NO era el commit que
    se testeó sino la punta de la rama al momento del evento. Ahora el commit
    viaja explícito desde ci.yml."""
    assert "sha: ${{ github.sha }}" in CI
    assert "DEPLOY_SHA: ${{ inputs.sha || github.sha }}" in DEPLOY
    assert "workflow_run.head_sha" not in _sin_comentarios(DEPLOY)


def test_el_tarball_se_sube_en_paralelo_con_los_tests():
    """El job `paquete` no depende de nadie: arranca junto con los tests, así
    cuando terminan el tarball ya está en S3 y el deploy sólo firma y manda."""
    bloque = _job(CI, "  paquete:", "  deploy:")
    assert "needs:" not in bloque, (
        "si `paquete` espera a los tests, vuelve a costar ~10 s de camino crítico")
    assert "empaquetar_y_subir_deploy.sh" in bloque
    # Y el deploy tiene que saber que no hace falta empaquetar de nuevo.
    assert "ya_subido: true" in CI


def test_el_tar_esta_escrito_una_sola_vez():
    """Lo llaman ci.yml y deploy.yml. La lista de exclusiones escrita dos veces
    se desactualiza sola y un día deja afuera algo que el server necesita."""
    assert "--exclude" not in _sin_comentarios(DEPLOY)
    assert "--exclude" not in _sin_comentarios(CI)
    assert "empaquetar_y_subir_deploy.sh" in DEPLOY


def test_el_redeploy_a_mano_sigue_existiendo():
    """Es la salida si un test flaky bloquea un deploy urgente. Y como en ese
    camino nadie subió el tarball, tiene que empaquetar él."""
    assert "workflow_dispatch:" in DEPLOY
    i = DEPLOY.index("- name: Empaquetar y subir a S3")
    assert "if: ${{ inputs.ya_subido != true }}" in DEPLOY[i - 200:i]


def test_los_tests_sin_base_no_levantan_postgres():
    """🚨 Los 16 s del contenedor estaban en el camino crítico de cada deploy
    para un job que no lo toca (los `not db` usan el FakeDB del conftest)."""
    job_test = _job(CI, "  test:", "  test-db:")
    assert "postgres" not in job_test, (
        "volvió el service postgres al job que no lo usa")
    job_db = _job(CI, "  test-db:", "  paquete:")
    assert "postgres:16-alpine" in job_db
    assert "ci-db" in job_db


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
