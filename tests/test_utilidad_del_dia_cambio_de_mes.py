"""El 1/10/2026 el mail decía "Utilidad del día −760.148" (Tamara 2026-10-01)."""
from __future__ import annotations

from datetime import date

from modules.informes import dia


def test_mismo_mes_es_la_resta():
    d = {"utilidad": 700_000, "fecha_ec": date(2026, 9, 29)}
    h = {"utilidad": 720_000, "fecha_ec": date(2026, 9, 30)}
    assert dia.d_utilidad_tramo(d, h) == 20_000


def test_cambio_de_mes_no_resta_el_mes_que_cerro(monkeypatch):
    llamado = []
    monkeypatch.setattr(dia, "_rows", lambda sql, p=None: llamado.append(p) or [{"utilidad": 805_423}])
    d = {"utilidad": 799_343, "fecha_ec": date(2026, 9, 30)}
    h = {"utilidad": 39_195, "fecha_ec": "2026-10-01"}
    # lo que faltaba de septiembre (805.423 − 799.343) + lo que va de octubre
    assert dia.d_utilidad_tramo(d, h) == 6_080 + 39_195
    assert llamado == [(2026, 10)]


def test_sin_foto_de_cierre_usa_lo_del_mes_nuevo(monkeypatch):
    monkeypatch.setattr(dia, "_rows", lambda sql, p=None: [])
    d = {"utilidad": 799_343, "fecha_ec": date(2026, 9, 30)}
    h = {"utilidad": 39_195, "fecha_ec": date(2026, 10, 1)}
    assert dia.d_utilidad_tramo(d, h) == 39_195


def test_si_la_base_falla_no_se_cae(monkeypatch):
    def boom(sql, p=None):
        raise RuntimeError("sin base")
    monkeypatch.setattr(dia, "_rows", boom)
    d = {"utilidad": 799_343, "fecha_ec": date(2026, 9, 30)}
    h = {"utilidad": 39_195, "fecha_ec": date(2026, 10, 1)}
    assert dia.d_utilidad_tramo(d, h) == 39_195


def test_fecha_rara_cae_en_la_resta():
    d = {"utilidad": 10, "fecha_ec": "xx"}
    h = {"utilidad": 30, "fecha_ec": None}
    assert dia.d_utilidad_tramo(d, h) == 20
    assert dia._mes_de(object()) is None


def test_el_mail_y_la_pantalla_usan_el_tramo():
    import inspect
    assert "d_utilidad_tramo(d, h)" in inspect.getsource(dia._resumen_rango)
    assert "d_utilidad_tramo(desde, hasta)" in inspect.getsource(dia.explicar)
