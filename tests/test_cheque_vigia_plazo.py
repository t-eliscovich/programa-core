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

    def fetch_one(self, sql, params=None, conn=None):
        return {"fechad": self.fechad_hoy} if getattr(self, "fechad_hoy", None) else None


def _correr(monkeypatch, malos, abiertos, fechad_hoy=None):
    avisos, resueltos = [], []
    fake = _DB(malos, abiertos)
    fake.fechad_hoy = fechad_hoy
    monkeypatch.setattr(vp, "db", fake)
    monkeypatch.setattr(avq, "archivar", lambda i, u="web", **k: resueltos.append((i, "archivado")) or True)
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
        "titulo": "Cheque 3616 de GUG a depositar el 31/12/2027: 444 días"}],
        fechad_hoy=date(2026, 12, 31))
    assert r == {"avisados": 0, "resueltos": 1}
    assert resueltos[0][1]["titulo"] == "Cheque 3616 de GUG: fecha corregida"


def test_la_fuente_tiene_nombre_y_permiso():
    assert avq.FUENTES["cheques"] == "Cheques"
    assert visibilidad.FUENTE_PERMISO["cheques"] == "cheques.ver"


def test_si_la_fecha_no_cambio_se_archiva_sin_decir_corregida(monkeypatch):
    r, _, hechos = _correr(monkeypatch, [], [{
        "id_aviso": 7, "clave": "plazo:99453",
        "titulo": "Cheque  de MSJ a depositar el 15/10/2026: 715 días"}],
        fechad_hoy=date(2026, 10, 15))
    assert hechos == [(7, "archivado")] and r["resueltos"] == 0


def test_un_postergado_cuenta_desde_la_postergacion():
    assert "fecha_postergacion" in vp._DESDE.split(",")[0]


def test_un_postergado_sin_fecha_de_postergacion_no_se_mide():
    import inspect
    src = inspect.getsource(vp.fuera_de_plazo)
    assert "c.fecha_postergacion IS NULL" in src
