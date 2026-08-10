"""`config/roles.py` contra `seguridad.permiso`: el drift que necesita la base.

`config/roles.py` se declara a sí mismo "the single source of truth", pero el
que manda en runtime es `seguridad.permiso`. Cambiar el archivo sin migración
deja producción EXACTAMENTE IGUAL y los tests en verde — pasó con las migs
0164/0165 y con `cupos.editar`.

El chequeo vive en `/admin/health/permisos-drift` y entra al `all` que corre el
cron. Los otros dos pares que tienen que coincidir —clases de Tailwind sin
compilar, links a pantallas que no existen— son ESTÁTICOS y se atajan en el CI
(`tests/test_drift_estatico.py`): ahí frenan el merge en vez de avisar al día
siguiente.

Medido el 2026-08-10: el código y la base coinciden rol por rol (misma huella
MD5 en los doce), así que este chequeo arranca callado.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_el_permiso_que_esta_en_el_repo_y_no_en_la_base_es_alerta():
    """⭐ La lección de las migs 0164/0165: `config/roles.py` se dice "single
    source of truth", pero el que manda en runtime es `seguridad.permiso`.
    Tocar el archivo sin migración deja producción igual y los tests en verde.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_drift_permisos

    alerts, stats = _evaluar_drift_permisos(
        codigo={"INT": {"cheques.ver", "cupos.editar"}},
        base={"INT": {"cheques.ver"}},
    )
    assert stats["permisos_solo_en_el_codigo"] == 1
    assert len(alerts) == 1 and alerts[0]["nivel"] == "HIGH"
    assert "sin migración" in alerts[0]["por_que"]


def test_el_permiso_que_sobra_en_la_base_avisa_pero_no_alarma():
    """No apaga ninguna pantalla: un ⚠ diario por eso entrena a ignorar."""
    from modules.admin_dbase.health_audit_view import _evaluar_drift_permisos

    alerts, stats = _evaluar_drift_permisos(
        codigo={"INT": {"cheques.ver"}},
        base={"INT": {"cheques.ver", "bancos.editar"}},
    )
    assert alerts == []
    assert stats["permisos_solo_en_la_base"] == 1
    assert stats["detalle_solo_en_la_base"] == [
        {"rol": "INT", "permisos": ["bancos.editar"]}]


def test_un_rol_que_vive_solo_en_el_repo_es_alerta():
    from modules.admin_dbase.health_audit_view import _evaluar_drift_permisos

    alerts, _ = _evaluar_drift_permisos(
        codigo={"INT": {"x"}, "Fantasma": {"y"}}, base={"INT": {"x"}})
    assert len(alerts) == 1
    assert "Fantasma" in alerts[0]["filas"]


def test_sin_drift_no_dice_nada():
    from modules.admin_dbase.health_audit_view import _evaluar_drift_permisos

    alerts, stats = _evaluar_drift_permisos(
        codigo={"INT": {"a", "b"}}, base={"INT": {"a", "b"}})
    assert alerts == []
    assert stats["permisos_solo_en_el_codigo"] == 0
    assert stats["permisos_solo_en_la_base"] == 0


def test_el_drift_de_permisos_entra_al_health_all():
    """Si no entra al `all`, no lo mira el cron y no lo mira nadie."""
    import inspect

    from modules.admin_dbase import health_audit_view

    src = " ".join(inspect.getsource(health_audit_view.health_all).split())
    assert "permisos_drift()" in src
    assert '"permisos_drift": data12' in src
    assert 'data12["ok"]' in src, "tiene que pesar en el ok general"
