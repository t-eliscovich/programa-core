"""La pantalla de /cheques/diag/cobros-duplicados.

Los tests del detector en sí (el SQL, contra un Postgres de verdad) están en
`tests/test_cheques_duplicados.py`. Acá se prueba lo otro: que la pantalla
**renderice** lo que el detector devuelve.

Eso no es redundante — es la lección del 04/08 con la tirilla de cobranza: el
módulo calculaba bien el número de factura y la plantilla imprimía un literal,
así que la frase salía sin el dato aunque los tests del módulo estuvieran todos
en verde. Un cálculo correcto que no llega a la pantalla no sirve de nada.
"""
from __future__ import annotations

from datetime import date

import bcrypt

from modules.cheques import duplicados as D

RUTA = "/cheques/diag/cobros-duplicados"


def _grupo(**kw):
    base = {
        "cli": "ELF", "imp": 735.0, "fechad": date(2026, 7, 13), "no_banco": 90,
        "n_cheques": 2, "n_cobranzas": 2, "n_a_mano": 2, "n_usuarios": 1,
        "ids": [100461, 100462], "numeros": ["—", "—"], "usuarios": ["alex"],
        "creados": [None, None], "stats": ["B", "B"],
        "ids_sin_respaldo": [100462], "ids_periodo_abierto": [],
        "ids_sin_dato": [], "veredicto": "1 de 2 no aparecen en el extracto",
        "monto_en_riesgo": 735.0,
    }
    base.update(kw)
    return base


def _login(client, fake_db):
    rid = fake_db.add_role("Dueño", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})


def _patch(monkeypatch, filas):
    monkeypatch.setattr(D, "revisar", lambda desde, hasta: list(filas))


def test_muestra_el_grupo_con_su_veredicto(client, fake_db, monkeypatch):
    _patch(monkeypatch, [_grupo()])
    _login(client, fake_db)
    html = client.get(RUTA).get_data(as_text=True)
    assert "ELF" in html
    assert "100461" in html and "100462" in html
    # ⭐ el veredicto tiene que salir del dato, no ser un texto fijo de la
    # plantilla: es lo que le dice a la dueña si tiene que hacer algo.
    assert "no aparecen en el extracto" in html


def test_el_monto_en_riesgo_es_el_que_calculo_el_detector(client, fake_db, monkeypatch):
    _patch(monkeypatch, [_grupo(imp=500.0, monto_en_riesgo=500.0,
                                ids=[1, 2, 3], ids_sin_respaldo=[3])])
    _login(client, fake_db)
    html = client.get(RUTA).get_data(as_text=True)
    assert "500,00" in html


def test_sin_hallazgos_lo_dice_en_verde(client, fake_db, monkeypatch):
    _patch(monkeypatch, [])
    _login(client, fake_db)
    html = client.get(RUTA).get_data(as_text=True)
    assert "Ninguna cobranza repetida" in html


def test_json(client, fake_db, monkeypatch):
    _patch(monkeypatch, [_grupo()])
    _login(client, fake_db)
    data = client.get(RUTA + "?formato=json").get_json()
    assert data["n"] == 1
    g = data["grupos"][0]
    assert g["cli"] == "ELF"
    assert g["fechad"] == "2026-07-13"
    assert g["ids"] == [100461, 100462]


def test_los_dias_se_acotan(client, fake_db, monkeypatch):
    """Un `?dias=99999` no puede barrer la tabla entera: la consulta corre
    dentro del request y el pool tiene pocas conexiones."""
    visto = {}

    def _fake(desde, hasta):
        visto["dias"] = (hasta - desde).days
        return []

    monkeypatch.setattr(D, "revisar", _fake)
    _login(client, fake_db)
    client.get(RUTA + "?dias=99999")
    assert visto["dias"] <= 400


def test_pide_permiso(client, fake_db):
    """Sin login no se ve — lista cobros y clientes por nombre."""
    resp = client.get(RUTA)
    assert resp.status_code in (301, 302)
