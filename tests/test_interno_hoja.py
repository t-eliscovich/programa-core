"""La oficina le saca los PDFs al portal — `/_interno/hoja` (fase 5 del plan
de memoria, 05/09/2026): un solo navegador headless para los dos procesos.

Lo que se protege:
  1. La puerta: sin secreto de máquina NO existe; con secreto, sólo desde
     127.0.0.1 y con la cabecera correcta. Cualquier otra cosa: 404.
  2. Lo que devuelve: los bytes del navegador de la oficina, o 204 si ese
     navegador no está (el portal cae a su camino viejo).
  3. El portal: no prende navegador propio y pide la hoja a la oficina; si
     la oficina no contesta, devuelve None (= camino viejo), nunca rompe.
"""
from __future__ import annotations

import pytest

from modules._lib import navegador
from modules.interno import views as interno

SECRETO = "s3cr3t0-de-prueba"


@pytest.fixture
def con_secreto(monkeypatch):
    monkeypatch.setenv(interno.VAR_SECRETO, SECRETO)


def _post(c, **kw):
    kw.setdefault("json", {"html": "<html>x</html>", "formato": "pdf"})
    kw.setdefault("environ_base", {"REMOTE_ADDR": "127.0.0.1"})
    kw.setdefault("headers", {interno.CABECERA: SECRETO})
    return c.post("/_interno/hoja", **kw)


def test_sin_secreto_configurado_la_puerta_no_existe(app, monkeypatch):
    monkeypatch.delenv(interno.VAR_SECRETO, raising=False)
    assert _post(app.test_client()).status_code == 404


def test_desde_otra_maquina_no_existe(app, con_secreto):
    r = _post(app.test_client(), environ_base={"REMOTE_ADDR": "190.152.1.7"})
    assert r.status_code == 404


def test_con_el_secreto_mal_no_existe(app, con_secreto):
    r = _post(app.test_client(), headers={interno.CABECERA: "otro"})
    assert r.status_code == 404


def test_devuelve_el_pdf_del_navegador_de_la_oficina(app, con_secreto, monkeypatch):
    visto = {}

    def _pdf(html, static, fondo=False):
        visto.update(html=html, fondo=fondo)
        return b"%PDF-1.4 oficina"

    monkeypatch.setattr(navegador, "pdf", _pdf)
    r = _post(app.test_client(), json={"html": "<p>hola</p>", "formato": "pdf", "fondo": True})
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.data == b"%PDF-1.4 oficina"
    assert visto == {"html": "<p>hola</p>", "fondo": True}


def test_devuelve_la_foto_con_sus_medidas(app, con_secreto, monkeypatch):
    visto = {}
    monkeypatch.setattr(navegador, "png", lambda html, static, ancho, alto: visto.update(ancho=ancho, alto=alto) or b"PNG")
    r = _post(app.test_client(), json={"html": "<p>x</p>", "formato": "png", "ancho": 900, "alto": 300})
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data == b"PNG"
    assert visto == {"ancho": 900, "alto": 300}


def test_si_el_navegador_de_la_oficina_no_esta_contesta_204(app, con_secreto, monkeypatch):
    monkeypatch.setattr(navegador, "pdf", lambda html, static, fondo=False: None)
    r = _post(app.test_client())
    assert r.status_code == 204 and r.data == b""


def test_un_pedido_incompleto_es_400(app, con_secreto):
    assert _post(app.test_client(), json={"html": "", "formato": "pdf"}).status_code == 400
    assert _post(app.test_client(), json={"html": "<p>x</p>", "formato": "doc"}).status_code == 400
    assert _post(app.test_client(), json={"html": "<p>x</p>", "formato": "png"}).status_code == 400


# --- el lado del portal -------------------------------------------------------


def test_el_portal_no_prende_navegador(monkeypatch):
    import modo

    monkeypatch.setenv("MODO", modo.PORTAL)
    monkeypatch.setattr(navegador, "_HILO", None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert navegador.arrancar_en_segundo_plano() is False
    assert navegador._HILO is None


def test_el_portal_le_pide_la_hoja_a_la_oficina(monkeypatch, con_secreto):
    import modo

    monkeypatch.setenv("MODO", modo.PORTAL)
    visto = {}

    class _R:
        status_code = 200
        content = b"%PDF-1.4 de la oficina"

    def _post(url, json=None, headers=None, timeout=None):
        visto.update(url=url, json=json, headers=headers, timeout=timeout)
        return _R()

    import requests

    monkeypatch.setattr(requests, "post", _post)
    from pathlib import Path

    assert navegador.png("<p>x</p>", Path("static"), 900, 300) == b"%PDF-1.4 de la oficina"
    assert visto["url"] == navegador.OFICINA_HOJA_URL
    assert visto["json"] == {"html": "<p>x</p>", "formato": "png", "fondo": False, "ancho": 900, "alto": 300}
    assert visto["headers"] == {interno.CABECERA: SECRETO}


def test_si_la_oficina_no_contesta_el_portal_sigue_por_el_camino_viejo(monkeypatch, con_secreto):
    import modo

    monkeypatch.setenv("MODO", modo.PORTAL)
    import requests

    def _post(*a, **k):
        raise requests.ConnectionError("5002 abajo")

    monkeypatch.setattr(requests, "post", _post)
    from pathlib import Path

    assert navegador.pdf("<p>x</p>", Path("static")) is None


def test_sin_secreto_el_portal_ni_lo_intenta(monkeypatch):
    import modo

    monkeypatch.setenv("MODO", modo.PORTAL)
    monkeypatch.delenv(interno.VAR_SECRETO, raising=False)
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("no había que pedir"))
    from pathlib import Path

    assert navegador.pdf("<p>x</p>", Path("static")) is None


def test_el_portal_no_registra_la_puerta_interna():
    import inspect

    import registro_erp
    import registro_portal

    assert "interno" in inspect.getsource(registro_erp)
    assert "interno" not in inspect.getsource(registro_portal)
