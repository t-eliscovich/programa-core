"""scripts/servidor/metabase_usuario_propio.py — el programa pasa a entrar a
Metabase con programa@intela.com.ec (TMT 2026-09-23)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_P = Path(__file__).resolve().parent.parent / "scripts" / "servidor" / "metabase_usuario_propio.py"
_spec = importlib.util.spec_from_file_location("metabase_usuario_propio", _P)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

URL = "http://localhost:3000"
ADMIN = {"METABASE_URL": URL, "METABASE_USERNAME": "tamara@x.com", "METABASE_PASSWORD": "vieja"}


class FakeMB:
    def __init__(self, usuarios=None, claves=None):
        self.claves = dict(claves if claves is not None else {"tamara@x.com": "vieja"})
        self.usuarios = usuarios if usuarios is not None else [
            {"id": 7, "email": "programa@intela.com.ec", "is_active": True, "is_superuser": False}]
        self.llamadas = []

    def __call__(self, metodo, url, cuerpo=None, token=None):
        self.llamadas.append((metodo, url.replace(URL, "")))
        if url.endswith("/api/session"):
            if self.claves.get(cuerpo["username"]) == cuerpo["password"]:
                return {"id": "tok-" + cuerpo["username"]}
            raise RuntimeError("401")
        if "/api/user?" in url:
            return {"data": self.usuarios}
        if url.endswith("/password"):
            self.claves["programa@intela.com.ec"] = cuerpo["password"]
            return {}
        raise AssertionError(url)


def test_pasa_al_usuario_propio_con_clave_nueva_probada():
    fake = FakeMB()
    msg, nuevos = m.correr(ADMIN, {}, http=fake)
    assert "pasa a entrar con programa@intela.com.ec" in msg
    assert nuevos["METABASE_USERNAME"] == "programa@intela.com.ec"
    assert fake.claves["programa@intela.com.ec"] == nuevos["METABASE_PASSWORD"]
    assert nuevos["METABASE_PASSWORD"] not in msg
    assert "'" not in nuevos["METABASE_PASSWORD"] and '"' not in nuevos["METABASE_PASSWORD"]


def test_si_ya_entra_con_el_propio_no_hace_nada():
    fake = FakeMB(claves={"programa@intela.com.ec": "k"})
    env = dict(ADMIN, METABASE_USERNAME="programa@intela.com.ec", METABASE_PASSWORD="k")
    assert m.correr(env, {}, http=fake) == ("ya entra con programa@intela.com.ec", None)
    fake2 = FakeMB(claves={})
    assert "NO anda" in m.correr(env, {}, http=fake2)[0]


@pytest.mark.parametrize("usuarios,frase", [
    ([], "no existe"),
    ([{"id": 7, "email": "programa@intela.com.ec", "is_active": False}], "desactivado"),
    ([{"id": 7, "email": "programa@intela.com.ec", "is_superuser": True}], "administrador"),
])
def test_ante_la_duda_no_toca(usuarios, frase):
    msg, nuevos = m.correr(ADMIN, {}, http=FakeMB(usuarios=usuarios))
    assert frase in msg and nuevos is None


def test_sin_credenciales_o_login_actual_roto_no_toca():
    assert m.correr({}, {}, http=FakeMB())[1] is None
    assert "no pudo entrar" in m.correr(ADMIN, {}, http=FakeMB(claves={}))[0]


def test_si_la_clave_nueva_no_entra_no_toca():
    class NoGuarda(FakeMB):
        def __call__(self, metodo, url, cuerpo=None, token=None):
            if url.endswith("/password"):
                return {}
            return super().__call__(metodo, url, cuerpo, token)
    msg, nuevos = m.correr(ADMIN, {}, http=NoGuarda())
    assert "no entra" in msg and nuevos is None


def test_errores_de_red_en_usuarios_y_clave():
    class Falla(FakeMB):
        def __init__(self, donde):
            super().__init__()
            self.donde = donde

        def __call__(self, metodo, url, cuerpo=None, token=None):
            if self.donde in url:
                raise RuntimeError("x")
            return super().__call__(metodo, url, cuerpo, token)
    assert "listar usuarios" in m.correr(ADMIN, {}, http=Falla("/api/user?"))[0]
    assert "poner la contraseña" in m.correr(ADMIN, {}, http=Falla("/password"))[0]


def test_la_de_maquina_gana_al_dotenv():
    ef = m.efectivas({"METABASE_USERNAME": "a"}, {"METABASE_USERNAME": "b", "METABASE_URL": "u"})
    assert ef["METABASE_USERNAME"] == "a" and ef["METABASE_URL"] == "u"


def test_dotenv_se_lee_y_se_reemplaza_sin_tocar_lo_demas(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# hola\nDB_HOST=x\nexport METABASE_USERNAME="t@x.com"\nMETABASE_PASSWORD=\'v\'\nroto\n', encoding="utf-8")
    d = m.leer_dotenv(f)
    assert d == {"DB_HOST": "x", "METABASE_USERNAME": "t@x.com", "METABASE_PASSWORD": "v"}
    assert m.leer_dotenv(tmp_path / "no") == {}
    nuevo = m.reemplazar_dotenv(f.read_text(), {"METABASE_USERNAME": "p", "METABASE_PASSWORD": "k", "OTRA": "1"})
    assert nuevo.splitlines() == ["# hola", "DB_HOST=x", "METABASE_USERNAME=p", "METABASE_PASSWORD=k", "roto", "OTRA=1"]


def test_main_nunca_corta_el_deploy(monkeypatch, capsys):
    monkeypatch.setattr(m, "leer_maquina", lambda: 1 / 0)
    assert m.main() == 0
    assert "AVISO" in capsys.readouterr().out
