"""Whitelist de `usuario_crea` del health audit.

TMT 2026-07-31 — el health quedaba en `ok=false` todos los días por markers
que YA estaban en producción y nunca se agregaron a `_USUARIOS_CONOCIDOS`
(bap-auto, asinfo-tejeduria, asinfo-hilo-local) más un usuario real
(federico). Un falso positivo diario entrena a ignorar el panel entero.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_markers_automaticos_en_la_whitelist():
    """Todo marker que escribe en producción tiene que estar acá."""
    from modules.admin_dbase.health_audit_view import _USUARIOS_CONOCIDOS

    for marker in (
        "bap-auto",           # anticipo USD → compra al recibirse la importación
        "asinfo-tejeduria",   # carga automática de producción de tejeduría
        "asinfo-hilo-local",  # carga automática de compras de hilo local
        "snapshot-diario",    # foto diaria del balance
    ):
        assert marker in _USUARIOS_CONOCIDOS, (
            f"marker {marker!r} escribe en producción pero no está en la "
            "whitelist → el health da ok=false todos los días por nada."
        )


def test_usuarios_reales_en_la_whitelist():
    from modules.admin_dbase.health_audit_view import _USUARIOS_CONOCIDOS

    for usuario in ("tamara", "andres", "alex", "federico"):
        assert usuario in _USUARIOS_CONOCIDOS
