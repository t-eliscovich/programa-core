"""El snapshot del Historial no se guarda si el químico viene del respaldo.

TMT 2026-07-30 (dueña: "nunca estuvo en 700"). El "Stock Quí." del balance cae
a `VQ0(mes anterior) + compras Q − ITIN` cuando el puente a formulas_app no
contesta, y ese `except: pass` no avisa. Una foto tomada en ese momento congela
un número plausible pero equivocado — y al cerrar el mes entra a la historia
para siempre. La columna EN VIVO se cura sola en la carga siguiente; la foto no.

El guard vive en `insertar_snapshot` a propósito: es el único cuello de botella
por el que una foto se vuelve permanente, así que cubre también el CLI y el
cierre de mes, no sólo el botón de la pantalla.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import snapshot_historia_mensual as snap  # noqa: E402

_FIS = "modules.informes.quimico_inv_formulas.quimico_total_fisico"


# ── quimico_respaldado_por_fisico ───────────────────────────────────────────
def test_con_fisico_disponible_da_true():
    with patch(_FIS, return_value=416_063.37):
        assert snap.quimico_respaldado_por_fisico(date(2026, 7, 30)) is True


@pytest.mark.parametrize("valor", [None, 0, 0.0, -1.0])
def test_sin_fisico_da_false(valor):
    # 0 y None son los dos disfraces del "no sé" en un bridge fail-soft.
    with patch(_FIS, return_value=valor):
        assert snap.quimico_respaldado_por_fisico(date(2026, 7, 30)) is False


def test_si_el_puente_explota_da_false():
    with patch(_FIS, side_effect=RuntimeError("formulas_db caído")):
        assert snap.quimico_respaldado_por_fisico(date(2026, 7, 30)) is False


# ── insertar_snapshot ───────────────────────────────────────────────────────
def test_insertar_se_planta_si_el_quimico_no_esta_respaldado():
    with pytest.raises(ValueError, match="stock de químicos"):
        snap.insertar_snapshot({"uqui": 559_700.0, "_quimico_fisico": False})


def test_insertar_no_escribe_nada_cuando_se_planta():
    """El plantón tiene que ser ANTES de tocar la base."""
    with patch.object(snap.db, "fetch_one") as fo, \
         patch.object(snap.db, "execute_returning") as er, \
         pytest.raises(ValueError):
        snap.insertar_snapshot({"uqui": 559_700.0, "_quimico_fisico": False})
    assert not fo.called
    assert not er.called


def test_la_marca_interna_no_llega_a_la_base():
    """`_quimico_fisico` es control de flujo, no una columna de scintela.historia."""
    vistos = {}

    def _er(sql, params=None, **kw):
        vistos.update(params or {})
        return {"id_historia": 1}

    with patch.object(snap.db, "fetch_one", return_value=None), \
         patch.object(snap.db, "execute_returning", side_effect=_er):
        snap.insertar_snapshot({"uqui": 416_063.37, "_quimico_fisico": True})
    assert not any(k.startswith("_") for k in vistos)


def test_backfill_de_meses_viejos_puede_saltearlo():
    """Sin la marca (o con True) pasa: el físico de 2024 ya no se reconstruye."""
    with patch.object(snap.db, "fetch_one", return_value=None), \
         patch.object(snap.db, "execute_returning", return_value={"id_historia": 7}):
        assert snap.insertar_snapshot({"uqui": 0}) == 7
