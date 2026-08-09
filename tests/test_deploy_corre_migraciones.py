"""El deploy corre las migraciones, en el orden correcto y con freno.

TMT 2026-08-09 (dueña): *"¿podríamos hacer que las migraciones corran junto
con el deploy?"*. Hasta hoy había que acordarse de abrir /admin/migraciones y
apretar el botón. Olvidarse daba un deploy VERDE con el cambio a medias — el
código nuevo arriba y la base sin tocar — que es justo la forma más difícil de
notar que algo falta: nada se rompe, simplemente el cambio no pasó. Pasó ese
mismo día con la 0182.

Lo que estos tests protegen no es que exista la línea, sino las dos decisiones
que la hacen segura:

  · **antes del restart**, para que el proceso nuevo nazca contra el schema
    que espera;
  · **si falla, no se reinicia**, para que quede el código viejo con el schema
    viejo — la única combinación coherente de las cuatro posibles. Reiniciar
    igual dejaría el código nuevo contra una base a medio migrar.

Un test que sólo buscara "migrate.py" pasaría con el paso puesto DESPUÉS del
restart, que es exactamente el bug que no queremos.
"""
from __future__ import annotations

from pathlib import Path

import pytest

DEPLOY = Path(".github/workflows/deploy.yml")


@pytest.fixture(scope="module")
def yml() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def test_el_deploy_corre_migrate(yml):
    assert "scripts\\migrate.py" in yml


def test_las_migraciones_corren_antes_de_reiniciar(yml):
    """El orden ES la decisión. Después del restart, el proceso nuevo arranca
    contra el schema viejo y se cae solo."""
    i_mig = yml.index("scripts\\migrate.py")
    i_stop = yml.index("Stop-ScheduledTask")
    assert i_mig < i_stop, "migrate.py quedó DESPUÉS del restart"


def test_si_la_migracion_falla_no_se_reinicia(yml):
    """Sin este freno, una migración a medias se combina con código nuevo."""
    i_mig = yml.index("scripts\\migrate.py")
    i_stop = yml.index("Stop-ScheduledTask")
    entre = yml[i_mig:i_stop]
    assert "LASTEXITCODE" in entre and "exit 1" in entre


def test_las_migraciones_viajan_en_el_tarball(yml):
    """No se puede correr lo que no se subió.

    `.github` sí está excluido —el workflow corre en GitHub, no en el EC2—
    pero `migrations/` y `scripts/` tienen que llegar enteros.
    """
    i = yml.index("--exclude")
    j = yml.index("programa_core.tar.gz", i)
    excluidos = yml[i:j]
    assert "migrations" not in excluidos
    assert "scripts" not in excluidos


def test_migrate_falla_con_codigo_distinto_de_cero():
    """El freno se apoya en que migrate.py NO se coma el error.

    `_apply_one` re-lanza la excepción después de loguear el FAIL, así que
    Python sale con 1. Si alguien la cambiara por un `print` y un `return`, el
    deploy seguiría de largo y este test es el que avisa.
    """
    src = Path("scripts/migrate.py").read_text(encoding="utf-8")
    i = src.index('print(f"  FAIL')
    assert "raise" in src[i : i + 200], (
        "migrate.py dejó de propagar el error: el deploy no se enteraría"
    )
