"""La bitácora de Metabase: saber si el puente contestó (TMT 2026-07-31).

Cuando la utilidad se movía, nadie podía decir si Asinfo había contestado — el
fallo se loguea por WARNING y ese log, en el servidor, no lo lee nadie. Ahora
cada consulta deja su huella en memoria y `/admin/health/metabase` la muestra.

Y de paso: el timeout de las consultas pesadas eran 20 s contra los 90 s de
`fetch_card`, sin ninguna razón.
"""
from unittest.mock import patch

import pytest

from modules._lib import metabase_client as mc


@pytest.fixture(autouse=True)
def _limpio():
    mc._BITACORA.clear()
    yield
    mc._BITACORA.clear()


class _Resp:
    status_code = 200

    def __init__(self, data=None):
        self._data = data or {"data": {"cols": [{"name": "n"}], "rows": [[1]]}}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _con_metabase(post):
    """fetch_dataset con el HTTP mockeado."""
    import types
    fake_requests = types.SimpleNamespace(post=post)
    return patch.dict("sys.modules", {"requests": fake_requests})


def test_una_consulta_buena_queda_anotada():
    with patch.object(mc, "disponible", return_value=True), \
         patch.object(mc, "_login", return_value="tok"), \
         patch.object(mc, "_url", return_value="http://mb"), \
         _con_metabase(lambda *a, **k: _Resp()):
        mc.fetch_dataset(2, "SELECT 1")
    b = mc.bitacora()
    assert len(b) == 1
    assert b[0]["ok"] is True and b[0]["db"] == 2
    assert b[0]["ms"] >= 0 and b[0]["error"] == ""


def test_un_fallo_queda_con_su_motivo():
    def explota(*a, **k):
        raise TimeoutError("read timeout")

    with patch.object(mc, "disponible", return_value=True), \
         patch.object(mc, "_login", return_value="tok"), \
         patch.object(mc, "_url", return_value="http://mb"), \
         _con_metabase(explota):
        filas = mc.fetch_dataset(2, "SELECT 1")
    assert filas == []
    b = mc.bitacora()
    assert b[0]["ok"] is False
    assert "read timeout" in b[0]["error"]


def test_la_bitacora_no_crece_para_siempre():
    for i in range(mc._BITACORA_MAX + 20):
        mc._anotar(2, i, True)
    assert len(mc.bitacora()) == mc._BITACORA_MAX
    # Queda la más NUEVA al final.
    assert mc.bitacora()[-1]["ms"] == mc._BITACORA_MAX + 19


def test_anotar_nunca_rompe():
    """Una bitácora no puede tumbar una consulta."""
    mc._anotar("no-es-un-numero", None, True)   # no levanta
    assert isinstance(mc.bitacora(), list)


def test_el_timeout_ya_no_son_20_segundos(monkeypatch):
    monkeypatch.delenv("METABASE_TIMEOUT_SECS", raising=False)
    assert mc._timeout_secs() == 45
    monkeypatch.setenv("METABASE_TIMEOUT_SECS", "90")
    assert mc._timeout_secs() == 90
    monkeypatch.setenv("METABASE_TIMEOUT_SECS", "3")      # fuera de rango
    assert mc._timeout_secs() == 45
    monkeypatch.setenv("METABASE_TIMEOUT_SECS", "ochenta")
    assert mc._timeout_secs() == 45


def test_la_hora_de_la_bitacora_es_la_de_ecuador():
    """El server corre en UTC: sin restar las 5 horas, la huella engaña."""
    import inspect
    src = inspect.getsource(mc._anotar)
    assert "5 * 3600" in src
