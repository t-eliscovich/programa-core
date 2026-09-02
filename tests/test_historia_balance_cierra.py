"""`/admin/health/historia-balance-cierra` -- Total Activo tiene que cerrar
contra Pasivo+Patrimonio en la última fila de scintela.historia.

Tamara 2026-09-02 ("no puede volver a pasar"): el incidente de agosto 2026
dejó anticipos/maquinaria/realty pisados con julio y desbalanceaba $976K
sin que nada avisara -- esta identidad hubiera prendido la alarma sola.
"""
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(row):
    with patch.object(hv.db, "fetch_one", return_value=row):
        vista = hv.historia_balance_cierra
        while hasattr(vista, "__wrapped__"):
            vista = vista.__wrapped__
        return vista()


FILA_AGOSTO_ROTA = {
    "id_historia": 517,
    "fecha": "2026-08-31",
    "banco": 1_675_042.40,
    "cart": 7_758_369.89,
    "anticipos": 2_159_970.67,   # julio, placeholder -- el bug real
    "ustock": 8_727_036.69,
    "uqui": 341_307.24,
    "maquinaria": 1_072_300.0,   # julio
    "realty": 2_379_014.0,       # julio
    "deuda": 3_355_956.28,
    "patrimonio": 21_732_772.07,
    "usuario_crea": "ancla-agosto-manual-2026-09-01",
}

FILA_AGOSTO_CORREGIDA = dict(
    FILA_AGOSTO_ROTA,
    anticipos=3_183_858.0,
    maquinaria=1_038_550.0,
    realty=2_364_564.0,
)


def test_detecta_el_desbalance_real_de_agosto(_app_ctx):
    resp = _run(FILA_AGOSTO_ROTA)
    data = resp.get_json()
    assert data["ok"] is False
    assert data["stats"]["delta"] < -900_000  # ~-976K
    assert data["alerts"][0]["category"] == "balance_no_cierra"


def test_no_alerta_una_vez_corregida(_app_ctx):
    resp = _run(FILA_AGOSTO_CORREGIDA)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["alerts"] == []
    assert abs(data["stats"]["delta"]) < 1000


def test_sin_filas_no_revienta(_app_ctx):
    resp = _run(None)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["alerts"] == []
