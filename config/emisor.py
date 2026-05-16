"""Datos fiscales del emisor (la fábrica) para facturación electrónica SRI.

Los datos del emisor son fijos — el RUC, razón social y dirección de la
fábrica no cambian con cada factura. En lugar de duplicarlos en cada
llamada al generador XML los leemos de acá.

Precedencia (de menor a mayor):
    1. Defaults "Intela" que viven en este archivo (seguro, reemplazar).
    2. Variables de entorno (SRI_EMISOR_*) — lo que usamos en producción.

Nota de seguridad: estos datos NO son secretos (el RUC y la razón social
aparecen en cada factura impresa). Las credenciales de firma (.p12 +
password) sí lo son y viven separadas — ver modules/sri/firma.py cuando
se implemente.
"""
from __future__ import annotations

import os

from modules.sri.xml import Emisor

# Defaults — reemplazar con env vars SRI_EMISOR_* antes de producción.
# Estos valores son los del RUC genérico; la primera vez que se despliegue,
# hay que cargar los reales en .env (SRI_EMISOR_RUC, etc.).
_DEFAULTS = {
    "ruc":                     "1790012345001",
    "razon_social":            "TEXTILES INTELA S.A.",
    "nombre_comercial":        "INTELA",
    "dir_matriz":              "Av. Panamericana Sur km 14, Quito, Ecuador",
    "dir_establecimiento":     "",                # vacío ⇒ None
    "obligado_contabilidad":   "SI",
    "contribuyente_especial":  "",                # vacío ⇒ None

    # Códigos de emisión — se usan también en la clave de acceso.
    "estab":                   "001",
    "pto_emi":                 "001",
}


def _get(key: str) -> str:
    env_key = f"SRI_EMISOR_{key.upper()}"
    return os.environ.get(env_key, _DEFAULTS[key]).strip()


def get_emisor() -> Emisor:
    """Construye el Emisor desde env + defaults. Fresco en cada llamada."""
    dir_est = _get("dir_establecimiento") or None
    contrib = _get("contribuyente_especial") or None
    return Emisor(
        ruc=_get("ruc"),
        razon_social=_get("razon_social"),
        nombre_comercial=_get("nombre_comercial"),
        dir_matriz=_get("dir_matriz"),
        dir_establecimiento=dir_est,
        obligado_contabilidad=_get("obligado_contabilidad").upper() or "SI",
        contribuyente_especial=contrib,
    )


def get_estab() -> str:
    return _get("estab").zfill(3)


def get_pto_emi() -> str:
    return _get("pto_emi").zfill(3)


def get_ambiente_default() -> str:
    """Ambiente SRI a usar si no se especifica uno.

    '1' = certificación (sandbox) — default, seguro.
    '2' = producción — override explícito con SRI_AMBIENTE=2.
    """
    amb = os.environ.get("SRI_AMBIENTE", "1").strip() or "1"
    if amb not in ("1", "2"):
        return "1"
    return amb
