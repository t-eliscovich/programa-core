"""Helpers stateless para TOTP (pyotp). Testeables sin Flask ni DB.

Separados de `views.py` para que los tests no necesiten importar Flask ni
tocar la base. El flujo completo (QR, confirm, verify en login) vive en
`views.py`.
"""
from __future__ import annotations

import pyotp

ISSUER = "Programa Core"


def generate_secret() -> str:
    """Secret base32 nuevo. Se guarda en seguridad.usuario.totp_secret."""
    return pyotp.random_base32()


def provisioning_uri(username: str, secret: str) -> str:
    """URI otpauth:// que Google Authenticator / Authy lee del QR.

    Ver RFC-6238 + Google Authenticator Key URI Format.
    """
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=ISSUER
    )


def verify(secret: str, code: str, *, valid_window: int = 1) -> bool:
    """Verifica un código de 6 dígitos contra el secret.

    `valid_window=1` permite ±30 segundos de drift de reloj — un balance
    razonable entre UX (el usuario escribe rápido y el código cambia) y
    seguridad (no se acepta un código muy viejo).
    """
    if not secret or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=valid_window)
    except Exception:
        return False
