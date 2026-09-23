"""El login a Metabase, de a uno y con freno — TMT 2026-09-23.

Ese día, después de un deploy, Metabase dejó de darle sesión al programa:
"Too many attempts! You must wait N seconds". Cada consulta a Asinfo pedía su
propio login varias veces por segundo, cada intento frenado sumaba espera, y la
espera pasó de 4,5 horas a 17 días: despachos, stock, precios y el sync de
clientes contestaban vacío. Estos tests fijan las tres reglas y la cura.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modules._lib import metabase_client as mc
from modules._lib import vigia_servidor as v

FRENADO = '{"errors":{"username":"Too many attempts! You must wait 16339 seconds before trying again."}}'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("METABASE_URL", "http://localhost:3000")
    monkeypatch.setenv("METABASE_USERNAME", "u@test.com")
    monkeypatch.setenv("METABASE_PASSWORD", "secret")
    mc._session_token = None
    yield
    mc._session_token = None


def _fake(status=200, texto="", token="tok1", queries=None):
    fake = MagicMock()
    login = MagicMock(status_code=status, text=texto)
    login.json.return_value = {"id": token}
    login.raise_for_status.return_value = None
    colas = list(queries or [])

    def post(url, **kw):
        if url.endswith("/api/session"):
            return login
        st, body = colas.pop(0) if colas else (200, {"data": {"cols": [], "rows": []}})
        r = MagicMock(status_code=st)
        r.json.return_value = body
        r.raise_for_status.side_effect = Exception(f"http {st}") if st >= 400 and st != 401 else None
        return r

    fake.post.side_effect = post
    return fake


def _logins(fake):
    return sum(1 for c in fake.post.call_args_list if c.args[0].endswith("/api/session"))


def test_con_sesion_viva_no_se_pide_otro_login():
    mc._session_token = "vivo"
    fake = _fake()
    assert mc._login(fake) == "vivo"
    assert _logins(fake) == 0


def test_el_login_fallido_frena_los_siguientes(monkeypatch):
    reloj = [1000.0]
    monkeypatch.setattr(mc, "_ahora", lambda: reloj[0])
    fake = _fake(status=401, texto='{"errors":{"password":"no"}}')
    for _ in range(20):
        with patch.dict("sys.modules", {"requests": fake}):
            assert mc.fetch_dataset(2, "SELECT 1") == []
    assert _logins(fake) == 1                      # no 20
    assert mc.estado_login()["frenado_s"] == 60
    assert mc.login_frenado_por_metabase() is False
    reloj[0] += 61                                 # pasó el freno: se vuelve a probar
    with patch.dict("sys.modules", {"requests": fake}):
        mc.fetch_dataset(2, "SELECT 1")
    assert _logins(fake) == 2


def test_el_freno_de_metabase_se_reconoce_y_frena_mas(monkeypatch):
    monkeypatch.setattr(mc, "_ahora", lambda: 1000.0)
    fake = _fake(status=400, texto=FRENADO)
    assert mc._login(fake) is None
    est = mc.estado_login()
    assert est["frenado_por_metabase"] is True
    assert est["frenado_s"] == 300
    assert "Too many attempts" in est["ultimo_error"]
    mc.destrabar_login()
    assert mc.estado_login()["ultimo_error"] == ""


def test_el_login_bueno_saca_el_freno(monkeypatch):
    monkeypatch.setattr(mc, "_ahora", lambda: 1000.0)
    assert mc._login(_fake(status=400, texto=FRENADO)) is None
    mc.destrabar_login()
    assert mc._login(_fake(token="nuevo")) == "nuevo"
    assert mc.estado_login() == {"hay_sesion": True, "ultimo_error": "",
                                 "frenado_s": 0, "frenado_por_metabase": False}


def test_login_sin_sesion_en_la_respuesta_es_un_fracaso():
    fake = _fake(token=None)
    assert mc._login(fake) is None
    assert "no devolvió sesión" in mc.estado_login()["ultimo_error"]


def test_401_renueva_una_sola_vez_aunque_otro_hilo_ya_la_haya_renovado():
    """El 401 no borra la sesión de todos: si al entrar al login ya hay OTRA
    sesión (la renovó otro hilo), se usa esa."""
    mc._session_token = "vieja"
    fake = _fake(token="renovada")
    assert mc._login(fake, vencido="vieja") == "renovada"
    assert _logins(fake) == 1
    assert mc._login(fake, vencido="vieja") == "renovada"   # ya renovada: no pide otra
    assert _logins(fake) == 1


def test_fetch_dataset_401_renueva_y_reintenta():
    mc._session_token = "vieja"
    fake = _fake(token="nueva", queries=[(401, None), (200, {"data": {"cols": [{"name": "a"}], "rows": [[1]]}})])
    with patch.dict("sys.modules", {"requests": fake}):
        assert mc.fetch_dataset(2, "SELECT 1") == [{"a": 1}]
    assert _logins(fake) == 1


def test_fetch_dataset_401_y_login_frenado():
    mc._session_token = "vieja"
    fake = _fake(status=400, texto=FRENADO, queries=[(401, None)])
    with patch.dict("sys.modules", {"requests": fake}):
        filas, ok = mc.fetch_dataset_estado(2, "SELECT 1")
    assert (filas, ok) == ([], False)


def test_el_error_del_login_queda_en_la_bitacora():
    mc._BITACORA.clear()
    mc._login(_fake(status=400, texto=FRENADO))
    assert mc.bitacora()[-1]["ok"] is False
    assert "login" in mc.bitacora()[-1]["error"]


def test_healthz_no_borra_la_sesion_de_todos(app):
    """Cada /healthz borraba la sesión compartida y pedía un login nuevo."""
    from modules._lib import formulas_db

    mc._session_token = "compartida"
    fake = _fake(token="otra")
    with patch.object(formulas_db, "disponible", return_value=False), \
         patch.dict("sys.modules", {"requests": fake}):
        r = app.test_client().get("/healthz/integraciones")
    assert r.get_json()["metabase"]["reachable"] is True
    assert mc._session_token == "compartida"
    assert _logins(fake) == 0


# --- el vigía reinicia Metabase cuando frenó al programa ---------------------

@pytest.fixture
def espia(monkeypatch):
    hecho = {"reinicios": 0, "avisos": []}
    monkeypatch.setattr(v, "_ultimo_reinicio_login", 0.0)
    monkeypatch.setattr(v, "_reiniciar_metabase",
                        lambda: (hecho.__setitem__("reinicios", hecho["reinicios"] + 1) or "Metabase reiniciado"))
    monkeypatch.setattr(v, "_avisar", lambda titulo, detalle, nivel="alerta", clave="":
                        hecho["avisos"].append(titulo) or {"titulo": titulo})
    return hecho


def test_vigia_no_hace_nada_si_el_login_anda(espia):
    assert v.vigilar_login_metabase(10_000.0) is None
    assert espia["reinicios"] == 0


def test_vigia_reinicia_metabase_si_freno_al_programa_una_vez_por_hora(espia):
    mc._login(_fake(status=400, texto=FRENADO))
    assert v.vigilar_login_metabase(10_000.0) == {"titulo": "Metabase frenó al programa"}
    assert espia["reinicios"] == 1
    assert mc.login_frenado_por_metabase() is False       # se sacó el freno propio
    mc._login(_fake(status=400, texto=FRENADO))           # sigue frenado a los 10 min
    assert v.vigilar_login_metabase(10_600.0) is None
    assert espia["reinicios"] == 1
    assert v.vigilar_login_metabase(13_700.0) is not None  # pasó la hora
    assert espia["reinicios"] == 2


def test_vigia_login_absorbe_errores(espia, monkeypatch):
    monkeypatch.setattr(mc, "login_frenado_por_metabase", lambda: 1 / 0)
    assert v.vigilar_login_metabase(10_000.0) is None


def test_health_metabase_muestra_el_login(app):
    """El 23/09 /admin/health/metabase decía ok con cero consultas: el login
    fallaba antes de consultar. Ahora el error del login se ve ahí."""
    from modules.admin_dbase import health_audit_view as h

    mc._login(_fake(status=400, texto=FRENADO))
    with app.test_request_context():
        body = h.metabase_bitacora.__wrapped__.__wrapped__().get_json()
    assert body["ok"] is False
    assert body["login"]["frenado_por_metabase"] is True
