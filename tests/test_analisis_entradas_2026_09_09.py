"""Entradas — qué día entró cada tela a la lista, sólo para la dueña.

Dueña 09/09/2026: *"me hacés una lista para mí a ver qué día entró qué tela
con sus kg. o sea visible solo para admin. quiero ver cuánta segunda va
entrando"*.
"""
from datetime import date

import pytest

from modules.analisis import queries, views


def _como(app, permisos):
    """Un cliente logueado con esos permisos. Flask no deja registrar dos
    `before_request` en el mismo test: el hook se registra una vez y lee los
    permisos de la app."""
    app.config["_PERMISOS_TEST"] = set(permisos)
    if not app.config.get("_LOGIN_TEST"):
        app.config["_LOGIN_TEST"] = True

        @app.before_request
        def _login():  # pragma: no cover - infra de test
            from flask import current_app, g, session
            session["usuario_id"] = 1
            g.user = {"id_usuario": 1, "username": "x", "id_rol": 1,
                      "nombre_rol": "r", "activo": True}
            g.permisos = set(current_app.config["_PERMISOS_TEST"])
    return app.test_client()


COHORTE = [
    {"fecha": date(2026, 9, 3), "subcategoria": "Fleece 102", "color": "CPA",
     "kg": 86.15, "motivo": "parado", "fuera": True, "categoria": "Fleece"},
    {"fecha": date(2026, 8, 20), "subcategoria": "Inter", "color": "BLA",
     "kg": 255.05, "motivo": "parado", "fuera": False, "categoria": "Jersey"},
    {"fecha": date(2026, 8, 20), "subcategoria": "Pique Nido", "color": "CRO",
     "kg": 40, "motivo": "segunda", "fuera": False, "categoria": "Pique"},
]


def test_el_resumen_por_dia_separa_parada_de_segunda(monkeypatch):
    """Lo que ella quiere mirar es cuánta SEGUNDA entra cada día: si se sumara
    todo junto, el número no contestaría la pregunta."""
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: COHORTE)
    r = queries.entradas()
    assert [d["fecha"] for d in r["dias"]] == [date(2026, 9, 3), date(2026, 8, 20)]
    d20 = r["dias"][1]
    assert (d20["telas"], d20["parada"], d20["segunda"]) == (2, 255.05, 40)
    assert r["dias"][0]["segunda"] == 0


def test_solo_la_duena_ve_la_pantalla(app, monkeypatch):
    """`analisis.ver` no alcanza (lo tiene INT desde el 25/08): la pantalla es
    la trazabilidad de la lista y va gateada con el permiso de administración,
    que sólo tienen los wildcard."""
    monkeypatch.setattr(queries, "entradas", lambda: {"filas": [], "dias": []})
    # Sin permiso la app contesta 404 a propósito (decisión de la dueña del
    # 22/05/2026: que no se vea que la sección existe).
    assert _como(app, {"analisis.ver"}).get("/analisis/entradas").status_code == 404
    assert _como(app, {"*"}).get("/analisis/entradas").status_code == 200


def test_la_apagada_se_muestra_y_dice_que_lo_esta(app, monkeypatch):
    """Entró ese día — el renglón no se borra. Pero hoy no cuenta, y un renglón
    que está sin decir qué le pasó se lee como un error."""
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: COHORTE)
    html = _como(app, {"*"}).get("/analisis/entradas").get_data(as_text=True)
    assert "Fleece 102" in html and "apagada" in html
    assert "255" in html and "03/09/26" in html


def test_el_menu_la_ofrece_solo_al_wildcard(app, monkeypatch):
    """Un link que da 404 no se ofrece."""
    monkeypatch.setattr(queries, "vendidos", lambda *a, **k: [])
    con = _como(app, {"*"}).get("/analisis/vendidos").get_data(as_text=True)
    assert 'href="/analisis/entradas"' in con
    sin = _como(app, {"analisis.ver"}).get("/analisis/vendidos").get_data(as_text=True)
    assert 'href="/analisis/entradas"' not in sin
    assert any(m["url"] == "/analisis/entradas" and m.get("admin")
               for m in views.MENU)


@pytest.mark.parametrize("permisos", [{"*"}, {"analisis.ver"}])
def test_el_inicio_tampoco_ofrece_la_tarjeta_sin_permiso(app, permisos):
    html = _como(app, permisos).get("/analisis").get_data(as_text=True)
    assert ("Entradas" in html) == ("*" in permisos)
