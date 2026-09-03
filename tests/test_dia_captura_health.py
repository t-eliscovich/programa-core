"""`/admin/health/dia-captura` — el ancla del día que dejó de clavarse.

🚨 El incidente que lo motiva (2026-09-03): el 02/09 `patant` entró a
`foto.COMPONENTES` como componente DERIVADO, y `dia.capturar()` —que recorre
esa lista contra `_CLAVE_BALANCE`— reventó con KeyError en cada tick.
`capturar()` NUNCA levanta (cuelga del hilo de fondo y no puede tumbar nada),
así que la mañana del 03/09 no hubo captura y nadie se enteró hasta que alguien
miró a mano.

`traza_fresca` NO lo caza: vigila `scintela.traza_utilidad`, que se siguió
grabando lo más bien. Lo que falló fue el ANCLA, que vive en otra tabla.

Este chequeo es el pedido del 2026-08-10 (*"y también algo que avise si no
está guardando"*) para ese otro lado.
"""
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv
from modules.informes import dia as _dia

# Los escenarios del dry run, en el orden en que se corrieron.
HOY = datetime(2026, 9, 3)


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(*, ahora, hechas=(), env=None):
    """Corre el chequeo con el reloj de Ecuador y la base puestos a mano."""
    envs = {"DIA_EXPLICACION": "1"}
    envs.update(env or {})
    filas = [{"momento": m, "creado_en": f"{ahora.date()} {h}"} for m, h in hechas]
    vista = hv.dia_captura_health
    while hasattr(vista, "__wrapped__"):
        vista = vista.__wrapped__
    with patch.dict(os.environ, envs, clear=False), \
         patch.object(_dia, "ahora_ec", lambda: ahora), \
         patch.object(_dia, "hoy_ec", lambda: ahora.date()), \
         patch.object(hv.db, "fetch_all", lambda *a, **k: filas):
        return vista().get_json()


def _cats(d):
    return {a["category"] for a in d["alerts"]}


def test_antes_de_la_hora_no_reclama_nada(_app_ctx):
    """06:40: a la captura de la mañana todavía no le toca."""
    d = _run(ahora=datetime(2026, 9, 3, 6, 40))
    assert d["ok"] is True
    assert d["stats"]["momentos"]["manana"]["atraso_min"] == -20.0


def test_con_la_captura_hecha_esta_ok(_app_ctx):
    d = _run(ahora=datetime(2026, 9, 3, 7, 2), hechas=[("manana", "07:01")])
    assert d["ok"] is True
    assert d["stats"]["momentos"]["manana"]["hecha"] is True
    assert d["stats"]["momentos"]["manana"]["creado_en"] == "2026-09-03 07:01"


def test_la_gracia_deja_pasar_un_deploy(_app_ctx):
    """07:20: el hilo puede estar arrancando después de un restart.

    Sin gracia, cada deploy a las 7 encendería la luz por dos minutos y la
    gente aprendería a ignorarla.
    """
    d = _run(ahora=datetime(2026, 9, 3, 7, 20))
    assert d["ok"] is True
    assert d["stats"]["gracia_min"] == 45


def test_el_incidente_del_03_09(_app_ctx):
    """10:05 del 03/09 sin captura: exactamente lo que pasó y nadie vio."""
    d = _run(ahora=datetime(2026, 9, 3, 10, 5))
    assert d["ok"] is False
    assert _cats(d) == {"dia_captura_faltante"}
    assert d["alerts"][0]["severity"] == "high"
    assert "manana" in d["alerts"][0]["msg"]
    assert d["stats"]["momentos"]["manana"]["atraso_min"] == 185.0
    # El cierre todavía no toca — una sola alerta, no dos.
    assert len(d["alerts"]) == 1


def test_a_media_tarde_el_cierre_todavia_no_toca(_app_ctx):
    d = _run(ahora=datetime(2026, 9, 3, 15, 0), hechas=[("manana", "07:01")])
    assert d["ok"] is True
    assert d["stats"]["momentos"]["cierre"]["hecha"] is False


def test_avisa_si_falta_el_cierre(_app_ctx):
    """19:50: sin el ancla del cierre la nota de la dueña queda sin ventana."""
    d = _run(ahora=datetime(2026, 9, 3, 19, 50), hechas=[("manana", "07:01")])
    assert d["ok"] is False
    assert "cierre" in d["alerts"][0]["msg"]


def test_el_dia_completo_no_alerta(_app_ctx):
    d = _run(ahora=datetime(2026, 9, 3, 20, 0),
             hechas=[("manana", "07:01"), ("cierre", "19:01")])
    assert d["ok"] is True
    assert d["alerts"] == []


def test_un_dia_entero_sin_capturar_avisa_por_las_dos(_app_ctx):
    d = _run(ahora=datetime(2026, 9, 3, 23, 30))
    assert d["ok"] is False
    assert len(d["alerts"]) == 2


def test_apagado_por_entorno_no_es_una_alarma(_app_ctx):
    """Con DIA_EXPLICACION=0 no hay captura POR DISEÑO: no es un problema."""
    d = _run(ahora=datetime(2026, 9, 3, 23, 30), env={"DIA_EXPLICACION": "0"})
    assert d["ok"] is True
    assert d["stats"] == {"apagado": True}


def test_respeta_el_horario_pisado_por_entorno(_app_ctx):
    """DIA_HORA_MANANA es como se prueba un horario nuevo sin tocar código.

    Con la mañana a las 9, a las 08:30 todavía no le toca — si el chequeo
    tuviera la hora hardcodeada, acá encendería la luz.
    """
    d = _run(ahora=datetime(2026, 9, 3, 8, 30), env={"DIA_HORA_MANANA": "9"})
    assert d["ok"] is True
    assert d["stats"]["momentos"]["manana"]["hora"] == 9


def test_entra_al_health_all(_app_ctx):
    """Una alarma que no está en /all no la mira nadie."""
    import inspect
    src = inspect.getsource(hv.health_all)
    assert "dia_captura_health()" in src
    assert '"dia_captura": data23' in src
    assert 'and data23["ok"]' in src
