"""Tests de modules.sri.firma.

Sin .p12 no podemos testear el happy path (firmar un XML real). Pero sí
verificamos que los errores se reportan correctamente y que
`firma_configurada()` refleja el estado del entorno.

Cuando se contrate la firma electrónica, agregar tests con un .p12
self-signed generado al vuelo (cryptography lo hace fácil).
"""
from __future__ import annotations

import os

import pytest

from modules.sri import firma


def test_firmar_xml_vacio_levanta():
    with pytest.raises(firma.FirmaFalloError):
        firma.firmar_xml(xml_str="", p12_path="/tmp/no-existe.p12", p12_password="x")


def test_firmar_xml_p12_no_existe_levanta_no_configurado(tmp_path):
    """Si el .p12 no existe en disco, es error de configuración."""
    # signxml puede o no estar instalado. Si no está, el test que espera
    # FirmaNoConfiguradaError es igual de válido.
    with pytest.raises(firma.FirmaNoConfiguradaError):
        firma.firmar_xml(
            xml_str="<factura/>",
            p12_path=str(tmp_path / "no-existe.p12"),
            p12_password="x",
        )


def test_firmar_xml_de_env_sin_env_levanta(monkeypatch):
    monkeypatch.delenv("SRI_P12_PATH", raising=False)
    with pytest.raises(firma.FirmaNoConfiguradaError):
        firma.firmar_xml_de_env("<factura/>")


def test_firma_configurada_false_sin_env(monkeypatch):
    monkeypatch.delenv("SRI_P12_PATH", raising=False)
    assert firma.firma_configurada() is False


def test_firma_configurada_false_con_p12_inexistente(monkeypatch, tmp_path):
    monkeypatch.setenv("SRI_P12_PATH", str(tmp_path / "ghost.p12"))
    assert firma.firma_configurada() is False


def test_firmar_xml_de_env_con_p12_inexistente_levanta(monkeypatch, tmp_path):
    monkeypatch.setenv("SRI_P12_PATH", str(tmp_path / "missing.p12"))
    monkeypatch.setenv("SRI_P12_PASSWORD", "x")
    with pytest.raises(firma.FirmaNoConfiguradaError):
        firma.firmar_xml_de_env("<factura/>")


def test_firmar_xml_con_password_bytes_no_rompe(tmp_path):
    """Si signxml está instalado, la versión que pasa password como bytes
    debe comportarse igual que la que la pasa como str. Si no, ambas caen
    en el check de p12 no existente."""
    with pytest.raises(firma.FirmaNoConfiguradaError):
        firma.firmar_xml(
            xml_str="<factura/>",
            p12_path=str(tmp_path / "no.p12"),
            p12_password=b"bytes-pwd",  # bytes en lugar de str
        )


def test_signxml_available_coherente_con_import():
    """Sanity check: _signxml_available debe reflejar el import real."""
    available = firma._signxml_available()
    try:
        import signxml  # noqa: F401
        assert available is True
    except ImportError:
        assert available is False
