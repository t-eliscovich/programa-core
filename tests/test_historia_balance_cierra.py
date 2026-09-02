"""`/admin/health/historia-balance-cierra` -- Total Activo tiene que cerrar
contra Pasivo+Patrimonio en scintela.historia.

Tamara 2026-09-02 ("no puede volver a pasar"): el incidente de agosto 2026
dejó anticipos/maquinaria/realty pisados con julio y desbalanceaba $976K
sin que nada avisara -- esta identidad hubiera prendido la alarma sola.

Segunda vuelta (Tamara, 2026-09-02 tarde): el chequeo miraba SÓLO la última
fila. Apenas se escribió la primera foto diaria de septiembre, la última fila
pasó a ser esa foto -- que se rehace todos los días y siempre cierra -- y el
cierre de agosto quedó fuera del radar para siempre. Justo la fila que
importaba: su `patrimonio` es el PATANT del que come la utilidad de todo el mes.
Ahora mira DOS filas: la última, y la de cierre que alimenta el PATANT.
"""
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(row, patant=...):
    """`row` = última fila; `patant` = fila de cierre (por defecto, la misma)."""
    filas = [row, row if patant is ... else patant]

    def _fake(sql, *a, **kw):
        # La 2ª consulta es la del PATANT (lleva el filtro por mes).
        return filas[1] if "date_trunc" in sql else filas[0]

    with patch.object(hv.db, "fetch_one", side_effect=_fake):
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
    assert data["stats"]["ultima"]["delta"] < -900_000  # ~-976K
    assert data["alerts"][0]["category"] == "balance_no_cierra"


def test_no_alerta_una_vez_corregida(_app_ctx):
    resp = _run(FILA_AGOSTO_CORREGIDA)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["alerts"] == []
    assert abs(data["stats"]["ultima"]["delta"]) < 1000


def test_sin_filas_no_revienta(_app_ctx):
    resp = _run(None)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["alerts"] == []


def test_un_cierre_roto_se_ve_aunque_la_ultima_fila_este_sana(_app_ctx):
    """El agujero real: la foto diaria de hoy cierra perfecto y tapa el cierre.

    Es exactamente lo que devolvía producción el 02/09 — ok:true mirando la
    foto `snapshot-diario` del día, con el cierre de agosto sin revisar.
    """
    foto_de_hoy = dict(FILA_AGOSTO_CORREGIDA,
                       id_historia=541, fecha="2026-09-02",
                       usuario_crea="snapshot-diario")
    resp = _run(foto_de_hoy, patant=FILA_AGOSTO_ROTA)
    data = resp.get_json()

    assert data["ok"] is False, "el cierre roto tiene que prender la alarma"
    assert abs(data["stats"]["ultima"]["delta"]) < 1000      # la foto cierra
    assert data["stats"]["patant"]["delta"] < -900_000       # el cierre no
    assert data["stats"]["patant"]["id_historia"] == 517
    assert "PATANT" in data["alerts"][0]["msg"]


def test_el_patrimonio_del_cierre_viaja_en_los_stats(_app_ctx):
    """Es el número que hay que poder leer sin abrir la base: fija la utilidad
    de todos los días del mes."""
    resp = _run(FILA_AGOSTO_CORREGIDA)
    data = resp.get_json()
    assert data["stats"]["patant"]["patrimonio"] == 21_732_772.07
