"""Ir más atrás que las últimas 200 fotos en /informes/traza.

TMT 2026-08-09: *"¿cuán atrás podemos ir? porque quizás quiero ver un finde
también"*. Guardadas están todas —nada se purga— pero la pantalla mostraba
siempre las últimas 200, que son ~20 h: el sábado no se alcanzaba nunca.

Lo que fija este archivo:
  · con desde/hasta la pantalla pide el RANGO, no las últimas 200;
  · una sola punta es un día suelto (y no "desde el sábado hasta hoy");
  · las fechas al revés se dan vuelta solas;
  · una fecha inventada NO deja la pantalla en blanco: vuelve a lo último;
  · el ancla (la foto anterior al rango, que existe sólo para que la fila más
    vieja tenga Δ) NO se muestra.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules.informes import traza as t


def _login(app, fake_db, permisos=("informes.ver",)):
    rid = fake_db.add_role("Informes", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _pantalla(app, fake_db, url, filas=()):
    c = _login(app, fake_db)
    entre = MagicMock(return_value=list(filas))
    ultimas = MagicMock(return_value=[])
    with patch.object(t, "entre", entre), patch.object(t, "ultimas", ultimas), \
         patch.object(t, "primera_foto", lambda: "2026-07-31"), \
         patch.object(t, "marcar_residuo", lambda f: f), \
         patch.object(t, "con_deltas", lambda f: list(f)):
        r = c.get(url)
    assert r.status_code == 200, r.data[:300]
    return r, entre, ultimas


def test_sin_fechas_sigue_mostrando_lo_ultimo(app, fake_db):
    _r, entre, ultimas = _pantalla(app, fake_db, "/informes/traza")
    ultimas.assert_called_once_with(200)
    entre.assert_not_called()


def test_con_desde_y_hasta_pide_el_rango(app, fake_db):
    _r, entre, ultimas = _pantalla(
        app, fake_db, "/informes/traza?desde=2026-08-01&hasta=2026-08-02")
    entre.assert_called_once_with("2026-08-01", "2026-08-02")
    ultimas.assert_not_called()


def test_una_sola_punta_es_un_dia_suelto(app, fake_db):
    """Pedir "desde el sábado" y contestar hasta hoy sería una tabla de mil
    filas que nadie pidió."""
    _r, entre, _u = _pantalla(app, fake_db, "/informes/traza?desde=2026-08-02")
    entre.assert_called_once_with("2026-08-02", "2026-08-02")


def test_las_fechas_al_reves_se_dan_vuelta(app, fake_db):
    _r, entre, _u = _pantalla(
        app, fake_db, "/informes/traza?desde=2026-08-03&hasta=2026-08-01")
    entre.assert_called_once_with("2026-08-01", "2026-08-03")


def test_una_fecha_inventada_no_deja_la_pantalla_en_blanco(app, fake_db):
    """🚨 `date.fromisoformat('ayer')` es un ValueError: sin el try la pantalla
    daba 500 por una URL vieja o un copy-paste."""
    _r, entre, ultimas = _pantalla(app, fake_db, "/informes/traza?desde=ayer")
    ultimas.assert_called_once_with(200)
    entre.assert_not_called()


def test_el_ancla_no_se_muestra(app, fake_db):
    """La foto anterior al rango entra en la consulta para que la fila más
    vieja tenga Δ, y se va antes de dibujar: si se mostrara, un rango de un
    día empezaría con una fila del día anterior."""
    filas = [
        {"id_traza": 9, "cuando": "02/08 09:00", "utilidad": 100.0,
         "en_rango": True, "delta": {}, "d_utilidad": 5.0, "residuo": None},
        {"id_traza": 8, "cuando": "01/08 23:55", "utilidad": 95.0,
         "en_rango": False, "delta": {}, "d_utilidad": None, "residuo": None},
    ]
    r, _e, _u = _pantalla(app, fake_db, "/informes/traza?desde=2026-08-02",
                          filas=filas)
    # La grilla parte `cuando`: la fecha sólo cuando cambia de día, y la hora
    # aparte. Por eso se busca la HORA, que es lo único que sale entero.
    html = r.data.decode()
    assert "09:00" in html
    assert "23:55" not in html
