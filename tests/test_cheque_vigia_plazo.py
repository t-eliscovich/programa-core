"""Campanita de cheques a depositar a más de 360 días (Tamara 13/10/2026:
"ponele campanita a Alex", por el 3616 de GUG)."""
from datetime import date

from modules.avisos import queries as avq
from modules.avisos import visibilidad
from modules.cheques import vigia_plazo as vp


class _DB:
    def __init__(self, malos, abiertos):
        self.malos, self.abiertos = malos, abiertos

    def fetch_all(self, sql, params=None, conn=None):
        return self.abiertos if "scintela.aviso" in sql else self.malos


def _correr(monkeypatch, malos, abiertos):
    avisos, resueltos = [], []
    monkeypatch.setattr(vp, "db", _DB(malos, abiertos))
    monkeypatch.setattr(avq, "avisar", lambda **k: avisos.append(k) or True)
    monkeypatch.setattr(avq, "resolver", lambda i, **k: resueltos.append((i, k)) or True)
    return vp.vigilar(forzar=True), avisos, resueltos


def test_avisa_el_3616_con_link_a_su_ficha(monkeypatch):
    r, avisos, _ = _correr(monkeypatch, [{
        "id_cheque": 106327, "no_cheque": "3616", "cod": "GUG",
        "importe": 10000, "fechad": date(2027, 12, 31), "dias": 444}], [])
    assert r == {"avisados": 1, "resueltos": 0}
    a = avisos[0]
    assert a["fuente"] == "cheques" and a["url"] == "/cheques/106327"
    assert a["clave"] == "plazo:106327"
    assert a["titulo"] == "Cheque 3616 de GUG a depositar el 31/12/2027: 444 días"
    assert "¿" not in a["detalle"]


def test_cuando_se_corrige_da_vuelta_el_aviso(monkeypatch):
    r, avisos, resueltos = _correr(monkeypatch, [], [{
        "id_aviso": 9, "clave": "plazo:106327",
        "titulo": "Cheque 3616 de GUG a depositar el 31/12/2027: 444 días"}])
    assert r == {"avisados": 0, "resueltos": 1}
    assert resueltos[0][1]["titulo"] == "Cheque 3616 de GUG: fecha corregida"


def test_la_fuente_tiene_nombre_y_permiso():
    assert avq.FUENTES["cheques"] == "Cheques"
    assert visibilidad.FUENTE_PERMISO["cheques"] == "cheques.ver"
