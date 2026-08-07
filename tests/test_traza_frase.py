"""La ventana contada en castellano.

TMT 2026-08-07: *"un botón 'explicame esta fila'… en vez de leer siete
columnas, un renglón que diga qué pasó"*.

🚨 Determinista, sin modelo: los números ya cierran al centavo contra el
balance, así que acá sólo se los ordena y se los nombra. Meter un modelo a
redactar sobre plata sería reintroducir la hipótesis en la pantalla que se hizo
justamente para sacarlas.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import frase  # noqa: E402


def _g(texto, aporte, familia="utilidad", **kw):
    return dict(texto=texto, aporte=aporte, familia=familia, **kw)


def test_dice_que_paso_y_cuanto():
    f = {"d_utilidad": 11399.30, "resumen": [
        _g("3 facturas nuevas · AJT, SAC, GBC", 11399.30)]}
    assert frase.explicar(f) == (
        "La utilidad subió $ 11.399: 3 facturas nuevas · AJT, SAC, GBC "
        "(+$ 11.399).")


def test_los_numeros_van_en_formato_ecuador():
    """Punto de miles, coma decimal — como todo el resto de la app."""
    f = {"d_utilidad": 1938.86, "resumen": [_g("2 facturas nuevas", 1938.86)]}
    assert "$ 1.939" in frase.explicar(f)
    assert "1,939" not in frase.explicar(f)


def test_una_cobranza_no_sube_la_utilidad_y_lo_dice():
    """🚨 Es lo que más se malinterpreta: la cobranza mueve DOS columnas y no
    aporta un peso. Sin la aclaración, la tabla parece decir que pasó algo
    grande y el Δ no lo acompaña."""
    f = {"d_utilidad": 1938.86, "resumen": [
        _g("2 facturas nuevas · JNC, HOM", 1938.86),
        _g("Cheque depositado · PGQ", 0.0, familia="traspaso", bruto=2366.37),
    ]}
    t = frase.explicar(f)
    assert "no movió la utilidad" in t
    assert "2.366" in t


def test_lo_ciego_se_nombra_sin_adornos():
    f = {"d_utilidad": 5000.0, "resumen": [
        _g("2 facturas nuevas", 4000.0),
        _g("Bancos: sin explicar por documento", 1000.0, familia="sin_explicar"),
    ]}
    t = frase.explicar(f)
    assert "sin poder atribuir a ningún documento" in t
    assert "$ 1.000" in t


def test_muchos_movimientos_se_resumen():
    f = {"d_utilidad": 1000.0, "resumen": [
        _g("uno", 400.0), _g("dos", 300.0), _g("tres", 200.0),
        _g("cuatro", 60.0), _g("cinco", 40.0)]}
    t = frase.explicar(f)
    assert "y otros 2, " in t
    assert "$ 100" in t
    assert " y y " not in t          # la conjunción no se duplica


def test_una_ventana_quieta_lo_dice_asi():
    assert frase.explicar({"d_utilidad": 0.0, "resumen": []}) == \
        "En esta ventana no se movió nada."


def test_una_foto_vieja_avisa_que_no_hay_documentos():
    f = {"sin_registro": True, "d_utilidad": -5044.0, "resumen": []}
    assert "anterior a la grabadora" in frase.explicar(f)


def test_el_ruido_de_centavos_no_entra_en_la_frase():
    f = {"d_utilidad": 4000.0, "resumen": [
        _g("2 facturas nuevas", 4000.0), _g("Stock", 0.4)]}
    assert "Stock" not in frase.explicar(f)


def test_sin_fila_no_inventa_nada():
    assert frase.explicar({}) == ""
    assert frase.explicar(None) == ""


def test_arranca_por_el_titular_y_no_por_la_lista():
    """🚨 Una enumeración de siete cosas es la tabla otra vez, y de eso se
    quería salir. Primero cuánto se movió, después por qué."""
    f = {"d_utilidad": -1569.0, "resumen": [
        {"texto": "Amortización del día", "aporte": -1125.0, "familia": "utilidad"},
        {"texto": "Terrenos/Edif.", "aporte": -482.0, "familia": "utilidad"}]}
    t = frase.explicar(f)
    assert t.startswith("La utilidad bajó $ 1.569:")
