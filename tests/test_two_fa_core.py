"""Tests para modules/two_fa/core.py — helpers puros, sin Flask ni DB."""
from __future__ import annotations

import time

import pyotp
import pytest

from modules.two_fa import core

# ---------------------------------------------------------------------------
# generate_secret
# ---------------------------------------------------------------------------

def test_generate_secret_es_base32_de_largo_esperado():
    s = core.generate_secret()
    # pyotp.random_base32() devuelve 32 caracteres base32 — suficiente
    # entropía (160 bits), compatible con Google Authenticator.
    assert isinstance(s, str)
    assert len(s) >= 16  # defensivo: cualquier longitud >=16 es válida
    # Base32 sólo usa A-Z y 2-7
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s)


def test_generate_secret_genera_valores_distintos():
    a = core.generate_secret()
    b = core.generate_secret()
    assert a != b, "dos secrets seguidos no deberían coincidir"


# ---------------------------------------------------------------------------
# provisioning_uri
# ---------------------------------------------------------------------------

def test_provisioning_uri_incluye_issuer_y_username():
    uri = core.provisioning_uri("tamara", "JBSWY3DPEHPK3PXP")
    assert uri.startswith("otpauth://totp/")
    # El URI codifica issuer en el path *y* como query param.
    assert "Programa%20Core" in uri or "Programa Core" in uri
    assert "tamara" in uri
    assert "secret=JBSWY3DPEHPK3PXP" in uri


# ---------------------------------------------------------------------------
# verify — código correcto / ventana de drift / entradas inválidas
# ---------------------------------------------------------------------------

def test_verify_acepta_codigo_actual():
    secret = core.generate_secret()
    code = pyotp.TOTP(secret).now()
    assert core.verify(secret, code) is True


def test_verify_rechaza_codigo_incorrecto():
    secret = core.generate_secret()
    # Un código obviamente incorrecto.
    assert core.verify(secret, "000000") is False


def test_verify_acepta_codigo_con_espacios():
    """Usuarios pegan "123 456" desde alguna app — lo limpiamos."""
    secret = core.generate_secret()
    code = pyotp.TOTP(secret).now()
    con_espacios = f"{code[:3]} {code[3:]}"
    assert core.verify(secret, con_espacios) is True


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34", None])
def test_verify_rechaza_entradas_malformadas(bad):
    secret = core.generate_secret()
    assert core.verify(secret, bad) is False


def test_verify_rechaza_secret_vacio():
    assert core.verify("", "123456") is False
    assert core.verify(None, "123456") is False


def test_verify_no_levanta_con_secret_invalido():
    """Si el secret está corrupto (no es base32), verify devuelve False sin romper."""
    assert core.verify("no-es-base32!!!", "123456") is False


def test_verify_valid_window_permite_codigo_previo():
    """Con valid_window=1, un código de hace 30s todavía es válido.

    Esto cubre el caso del usuario que tarda en escribir y el código rota.
    """
    secret = core.generate_secret()
    # Generar el código del período anterior (30s atrás).
    prev_code = pyotp.TOTP(secret).at(int(time.time()) - 30)
    assert core.verify(secret, prev_code, valid_window=1) is True


def test_verify_valid_window_cero_rechaza_codigo_previo():
    """Con valid_window=0, sólo el código actual vale — útil para testing."""
    secret = core.generate_secret()
    prev_code = pyotp.TOTP(secret).at(int(time.time()) - 60)
    # El de hace un minuto ya no entra en la ventana.
    assert core.verify(secret, prev_code, valid_window=0) is False
