"""Tests del importador de colores desde formulas_app al catálogo de tintura.

modules.admin_dbase.tinto_costos_sync.refresh_from_formulas_app trae TODAS las
fórmulas/colores de formulas_app (bridge read-only) y las upsertea a
scintela.tinto_costos:
    - inserta los códigos faltantes (costo 0),
    - rellena el color si en el catálogo está vacío,
    - NO pisa costos ni colores ya cargados (conservador),
    - cod normalizado a UPPER + truncado a 5 (igual que /informes/tinto-carga).

Nunca tocamos una DB real: mockeamos formulas_db.fetch_all (la fuente) y el
módulo db (el destino).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules.admin_dbase import tinto_costos_sync as tcs

# ---------------------------------------------------------------------------
# _catalogo_desde_formulas_app
# ---------------------------------------------------------------------------

def test_catalogo_bridge_no_disponible_devuelve_vacio():
    with patch("modules._lib.formulas_db.disponible", return_value=False):
        assert tcs._catalogo_desde_formulas_app() == {}


def test_catalogo_mapea_cod_color_categoria():
    rows = [
        {"cod": "azu", "color": "AZUL", "categoria": "Color Fuerte"},
        {"cod": "LIF", "color": "LILA FUERTE", "categoria": ""},
    ]
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_all", return_value=rows):
        cat = tcs._catalogo_desde_formulas_app()
    # cod en UPPER
    assert "AZU" in cat and "LIF" in cat
    # categoría se anexa al color si entra en 30 chars
    assert cat["AZU"]["color"] == "AZUL · Color Fuerte"
    assert cat["LIF"]["color"] == "LILA FUERTE"
    # costo siempre 0 (formulas_app no tiene $/kg de tela)
    assert cat["AZU"]["costo"] == 0.0


def test_catalogo_trunca_cod_a_5_y_salta_man():
    rows = [
        {"cod": "ABCDEFGH", "color": "X", "categoria": ""},
        {"cod": "MAN", "color": "MANUAL", "categoria": ""},
        {"cod": "", "color": "VACIO", "categoria": ""},
    ]
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_all", return_value=rows):
        cat = tcs._catalogo_desde_formulas_app()
    assert "ABCDE" in cat
    assert "MAN" not in cat
    assert "" not in cat


def test_catalogo_color_largo_no_anexa_categoria():
    rows = [{"cod": "ROJ", "color": "R" * 28, "categoria": "Color Fuerte"}]
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_all", return_value=rows):
        cat = tcs._catalogo_desde_formulas_app()
    # no entra la categoría → solo el color, truncado a 30
    assert cat["ROJ"]["color"] == ("R" * 28)


# ---------------------------------------------------------------------------
# refresh_from_formulas_app
# ---------------------------------------------------------------------------

def _drain(gen):
    return "\n".join(list(gen))


def test_refresh_sin_bridge_no_escribe():
    fake_db = MagicMock()
    with patch.dict("sys.modules", {"db": fake_db}), \
         patch.object(tcs, "_catalogo_desde_formulas_app", return_value={}):
        out = _drain(tcs.refresh_from_formulas_app(aplicar=True))
    assert "no disponible" in out
    fake_db.execute.assert_not_called()


def test_refresh_inserta_faltantes_y_no_pisa_existentes():
    cat = {
        "AZU": {"color": "AZUL", "costo": 0.0},        # nuevo → INSERT
        "LIF": {"color": "LILA", "costo": 0.0},        # existe con color → no toca
        "GRC": {"color": "GRIS", "costo": 0.0},        # existe con color vacío → rellena
    }
    fake_db = MagicMock()
    fake_db.fetch_one.return_value = {"t": "scintela.tinto_costos"}
    fake_db.fetch_all.return_value = [
        {"cod": "LIF", "color": "LILA YA", "costo": 5.5},
        {"cod": "GRC", "color": "", "costo": 3.0},
    ]
    with patch.dict("sys.modules", {"db": fake_db}), \
         patch.object(tcs, "_catalogo_desde_formulas_app", return_value=cat):
        out = _drain(tcs.refresh_from_formulas_app(aplicar=True))

    sqls = [c.args[0] for c in fake_db.execute.call_args_list]
    inserts = [s for s in sqls if "INSERT" in s]
    updates = [s for s in sqls if "UPDATE" in s]
    # 1 insert (AZU), 1 update color (GRC), nada para LIF
    assert len(inserts) == 1
    assert len(updates) == 1
    # el insert es del código nuevo AZU
    azu_call = [c for c in fake_db.execute.call_args_list if "INSERT" in c.args[0]][0]
    assert azu_call.args[1][0] == "AZU"
    assert "+1 nuevos" in out
    assert "1 colores rellenados" in out


def test_refresh_dry_run_no_escribe():
    cat = {"AZU": {"color": "AZUL", "costo": 0.0}}
    fake_db = MagicMock()
    fake_db.fetch_one.return_value = {"t": "scintela.tinto_costos"}
    fake_db.fetch_all.return_value = []
    with patch.dict("sys.modules", {"db": fake_db}), \
         patch.object(tcs, "_catalogo_desde_formulas_app", return_value=cat):
        out = _drain(tcs.refresh_from_formulas_app(aplicar=False))
    fake_db.execute.assert_not_called()
    assert "DRY-RUN" in out


def test_refresh_tabla_inexistente_se_omite():
    cat = {"AZU": {"color": "AZUL", "costo": 0.0}}
    fake_db = MagicMock()
    fake_db.fetch_one.return_value = {"t": None}  # tabla no existe
    with patch.dict("sys.modules", {"db": fake_db}), \
         patch.object(tcs, "_catalogo_desde_formulas_app", return_value=cat):
        out = _drain(tcs.refresh_from_formulas_app(aplicar=True))
    assert "no existe" in out
    fake_db.execute.assert_not_called()
