"""Tests de modules.sri.envio — envelope SOAP + parseo de respuestas."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from modules.sri import envio

# ---------------------------------------------------------------------------
# URLs por ambiente
# ---------------------------------------------------------------------------

def test_url_recepcion_certificacion():
    assert "celcer.sri.gob.ec" in envio.url_recepcion("1")


def test_url_recepcion_produccion():
    assert "cel.sri.gob.ec" in envio.url_recepcion("2")
    assert "celcer.sri.gob.ec" not in envio.url_recepcion("2")


def test_url_autorizacion_certificacion():
    assert "celcer.sri.gob.ec" in envio.url_autorizacion("1")


# ---------------------------------------------------------------------------
# Validaciones de entrada
# ---------------------------------------------------------------------------

def test_enviar_a_recepcion_xml_vacio_levanta():
    with pytest.raises(envio.EnvioFalloError):
        envio.enviar_a_recepcion(xml_firmado="", ambiente="1")


def test_consultar_autorizacion_clave_invalida_levanta():
    with pytest.raises(envio.EnvioFalloError):
        envio.consultar_autorizacion(clave_acceso="corto", ambiente="1")
    with pytest.raises(envio.EnvioFalloError):
        envio.consultar_autorizacion(clave_acceso="", ambiente="1")


# ---------------------------------------------------------------------------
# Parsing de respuestas — no requieren network
# ---------------------------------------------------------------------------

def test_parse_recepcion_recibida():
    body = """<?xml version="1.0"?>
    <ns:respuesta xmlns:ns="x">
      <estado>RECIBIDA</estado>
    </ns:respuesta>"""
    result = envio._parse_recepcion(body)
    assert result["estado"] == "RECIBIDA"
    assert result["mensajes"] == []
    assert result["raw"] == body


def test_parse_recepcion_devuelta_con_mensajes():
    body = """<?xml version="1.0"?>
    <respuesta>
      <estado>DEVUELTA</estado>
      <comprobantes>
        <comprobante>
          <mensajes>
            <mensaje>
              <identificador>43</identificador>
              <mensaje>RUC sin autorización</mensaje>
              <informacionAdicional>RUC 1790012345001</informacionAdicional>
              <tipo>ERROR</tipo>
            </mensaje>
          </mensajes>
        </comprobante>
      </comprobantes>
    </respuesta>"""
    result = envio._parse_recepcion(body)
    assert result["estado"] == "DEVUELTA"
    assert len(result["mensajes"]) == 1
    assert result["mensajes"][0]["identificador"] == "43"
    assert "RUC" in result["mensajes"][0]["mensaje"]


def test_parse_autorizacion_autorizado():
    body = """<?xml version="1.0"?>
    <respuesta>
      <autorizaciones>
        <autorizacion>
          <estado>AUTORIZADO</estado>
          <numeroAutorizacion>1234567890123456789012345678901234567890123456789</numeroAutorizacion>
          <fechaAutorizacion>2026-04-17T14:35:12.345-05:00</fechaAutorizacion>
        </autorizacion>
      </autorizaciones>
    </respuesta>"""
    result = envio._parse_autorizacion(body)
    assert result["estado"] == "AUTORIZADO"
    assert result["numero_autorizacion"] == "1234567890123456789012345678901234567890123456789"
    assert isinstance(result["fecha_autorizacion"], datetime)


def test_parse_autorizacion_en_proceso():
    body = "<respuesta><autorizaciones></autorizaciones><estado>EN PROCESO</estado></respuesta>"
    result = envio._parse_autorizacion(body)
    assert result["estado"] == "EN PROCESO"
    assert result["numero_autorizacion"] is None
    assert result["fecha_autorizacion"] is None


def test_parse_autorizacion_desconocido_cuando_no_hay_estado():
    """Respuesta malformada: marcar DESCONOCIDO para que el caller re-intente."""
    body = "<respuesta></respuesta>"
    result = envio._parse_autorizacion(body)
    assert result["estado"] == "DESCONOCIDO"


def test_parse_autorizacion_fecha_sin_timezone():
    """Algunas respuestas del SRI vienen sin timezone."""
    body = ("<respuesta><estado>AUTORIZADO</estado>"
            "<fechaAutorizacion>2026-04-17T14:35:12</fechaAutorizacion>"
            "</respuesta>")
    result = envio._parse_autorizacion(body)
    assert result["estado"] == "AUTORIZADO"
    assert isinstance(result["fecha_autorizacion"], datetime)


# ---------------------------------------------------------------------------
# HTTP mock — envío no toca la red
# ---------------------------------------------------------------------------

def test_enviar_a_recepcion_happy_path(monkeypatch):
    """Mock requests.post para que _http_post devuelva lo esperado."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<respuesta><estado>RECIBIDA</estado></respuesta>"

    import requests  # viene de un dep transitivo (Flask no lo trae — puede faltar en CI)
    with patch.object(requests, "post", return_value=fake_resp) as mock_post:
        result = envio.enviar_a_recepcion(xml_firmado="<factura/>", ambiente="1")
        assert result["estado"] == "RECIBIDA"
        # Verificar que el body incluye base64 y la URL es de certificación
        args, kwargs = mock_post.call_args
        assert "celcer.sri.gob.ec" in args[0] or "celcer.sri.gob.ec" in kwargs.get("url", args[0] if args else "")
        assert "text/xml" in kwargs["headers"]["Content-Type"]


def test_consultar_autorizacion_pasa_clave_al_envelope(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<respuesta><estado>EN PROCESO</estado></respuesta>"
    import requests
    with patch.object(requests, "post", return_value=fake_resp) as mock_post:
        clave = "0" * 49
        envio.consultar_autorizacion(clave_acceso=clave, ambiente="1")
        sent_data = mock_post.call_args.kwargs["data"]
        assert clave.encode() in sent_data


def test_http_post_4xx_no_reintenta(monkeypatch):
    """4xx es error del cliente — no retry, failfast."""
    monkeypatch.setenv("SRI_REINTENTOS", "3")
    fake_resp = MagicMock()
    fake_resp.status_code = 400
    fake_resp.text = "Bad Request"

    import requests
    with patch.object(requests, "post", return_value=fake_resp) as mock_post:
        with pytest.raises(envio.EnvioFalloError) as exc:
            envio._http_post("http://fake/endpoint", "<envelope/>")
        assert mock_post.call_count == 1  # sólo UN intento
        assert "400" in str(exc.value)


def test_http_post_5xx_reintenta(monkeypatch):
    """5xx es transient — reintentar hasta SRI_REINTENTOS."""
    monkeypatch.setenv("SRI_REINTENTOS", "3")
    monkeypatch.setenv("SRI_REINTENTO_BACKOFF_SEG", "0")  # no sleep en test
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "Server busy"

    import requests
    with patch.object(requests, "post", return_value=fake_resp) as mock_post:
        with pytest.raises(envio.EnvioFalloError):
            envio._http_post("http://fake/endpoint", "<envelope/>")
        assert mock_post.call_count == 3  # reintentó todas las veces


def test_http_post_timeout_reintenta(monkeypatch):
    monkeypatch.setenv("SRI_REINTENTOS", "2")
    monkeypatch.setenv("SRI_REINTENTO_BACKOFF_SEG", "0")
    import requests
    with patch.object(requests, "post", side_effect=requests.Timeout("slow")) as mock_post:
        with pytest.raises(envio.EnvioFalloError):
            envio._http_post("http://fake/endpoint", "<envelope/>")
        assert mock_post.call_count == 2
