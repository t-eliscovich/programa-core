"""El renglón del anticipo se queda con el lote; lo demás queda con nombre.

Tamara 18/09/2026, ventana 10:56 → 11:02 (fotos 11822 → 11823, AC 40):
la pantalla decía UN renglón —"AC 40 · entró la mercadería de 4 anticipos
−3.021"— y adentro llevaba 833 kg de tejido y terminado que se fueron por la
puerta en esa misma ventana (−4.215). *"Si una regla absorbe lo que no
explica, la pantalla dice 'todas explicadas' y no es cierto. Eso vale más
que este caso puntual."*

Ahora son tres, y suman lo mismo:

    AC 40 · entró la mercadería de 4 anticipos   Ant −82.829 / Stk +82.829   0
    revaluó el stock de hil.                     +1.194   ($/kg 3,0951 → 3,0983)
    salió de tej. y term.                        −4.215

Los números son los reales de esa ventana (traza_utilidad + dia_movimiento).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import traza as t  # noqa: E402

# Las dos fotos de la traza (sólo lo que `stock_de_la_ventana` lee).
FOTO_1056 = {"id_traza": 11822, "compras_kg": 181253.76, "hilado_ukg": 3.0951,
             "hilado_kg": 1960511.30, "tejido_kg": 286084.61,
             "terminado_kg": 311156.01}
FOTO_1102 = {"id_traza": 11823, "compras_kg": 205748.00, "hilado_ukg": 3.0983,
             "hilado_kg": 1985005.54, "tejido_kg": 285999.81,
             "terminado_kg": 310408.51}

_EV = {tipo: {"tipo": "bap_anticipo_a_compra", "grupo": "g40",
              "label": "Anticipo → Compra", "id_mov_doble": 4040,
              "dia": "2026-09-18",
              "concepto": "AC 40 · 4 anticipo(s) → compra #10230",
              "meta": {"codigo_prov": "AC", "numero_compra": 10230,
                       "n_anticipos": 4}}
       for tipo in ("d3222", "d3378", "d2877", "d3095")}


def _movs():
    """Los seis movimientos reales de la foto 11823 (18/09 11:02 EC)."""
    ant = [("d3222", -56363.06), ("d3378", -18261.44), ("d2877", -7897.60),
           ("d3095", -307.10)]
    return [
        *[{"componente": "antic", "doc_id": d, "tipo": "baja",
           "etiqueta": "Anticipo AC · 40/26", "regla": "Anticipo aplicado",
           "aporte": a, "familia": "traspaso"} for d, a in ant],
        {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
         "etiqueta": "salió de tej. y term. · entró a hil.", "regla": "Stock",
         "aporte": 71597.19, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
         "etiqueta": "cambió el $/kg de 3 etapas",
         "regla": "Revaluación de stock", "aporte": 8211.37,
         "familia": "utilidad"},
    ]


D_UTILIDAD = -3020.63


def _resumen(movs=None, stock="real", d_utilidad=D_UTILIDAD):
    if stock == "real":
        stock = t.stock_de_la_ventana(FOTO_1102, FOTO_1056)
    with patch.object(t.db, "fetch_all", return_value=[]):
        return t.resumir(movs or _movs(), d_utilidad, _EV,
                         hasta="2026-09-18T16:02:44", stock=stock)


def test_la_ventana_lee_el_lote_de_las_dos_fotos():
    s = t.stock_de_la_ventana(FOTO_1102, FOTO_1056)
    assert s["lote_kg"] == pytest.approx(24494.24)
    assert s["p0"] == 3.0951 and s["p1"] == 3.0983
    assert s["dkg"]["tejido"] == pytest.approx(-84.80)
    assert s["dkg"]["terminado"] == pytest.approx(-747.50)


def test_sin_lote_o_sin_fotos_no_hay_stock():
    assert t.stock_de_la_ventana(FOTO_1102, None) == {}
    assert t.stock_de_la_ventana(FOTO_1102, FOTO_1102) == {}   # lote_kg = 0
    assert t.stock_de_la_ventana(dict(FOTO_1102, hilado_ukg=None), FOTO_1056) == {}
    assert t.stock_de_la_ventana(dict(FOTO_1102, tejido_kg=None), FOTO_1056) == {}


def test_una_ventana_con_anticipo_y_salida_de_tejido_da_tres_renglones():
    out = _resumen()
    textos = [g["texto"] for g in out]
    assert textos == ["AC 40 · entró la mercadería de 4 anticipos",
                      "salió de tej. y term.",
                      "revaluó el stock de hil."], textos
    lote, resto, reval = out
    # 1. El lote: la plata cambia de anticipos a stock y no aporta nada.
    assert lote["aporte"] == 0.0
    assert lote["por_col"] == {"antic": -82829.20, "vsto": 82829.20}
    assert lote["bruto"] == 82829.20
    assert lote["nota"] == "24.494 kg a $ 3,3816 el kilo"
    assert lote["col"] == "vsto"                  # acá se pintan los kilos
    # 2. La revaluación, con nombre y su $/kg.
    assert reval["aporte"] == pytest.approx(75812.12 + 8211.37 - 82829.20)
    assert reval["nota"] == "$/kg de hil.: 3,0951 → 3,0983"
    assert reval["familia"] == "utilidad"
    # 3. El resto del stock: los 833 kg que salieron, sin el "entró a hil."
    assert resto["aporte"] == pytest.approx(71597.19 - 75812.12)
    assert resto["por_col"] == {"vsto": pytest.approx(71597.19 - 75812.12)}
    # Y la suma de los tres es el Δ de la ventana: nada se pierde.
    assert sum(g["aporte"] for g in out) == pytest.approx(D_UTILIDAD, abs=0.02)
    assert not any(g["texto"] == "diferencia contra el Δ" for g in out)


def test_sin_las_fotos_sigue_como_antes():
    """Fotos viejas sin kilos: el renglón único de siempre, no se rompe nada."""
    out = _resumen(stock=None)
    assert len(out) == 1, [g["texto"] for g in out]
    assert out[0]["texto"] == "AC 40 · entró la mercadería de 4 anticipos"
    assert out[0]["aporte"] == pytest.approx(D_UTILIDAD, abs=0.02)


def test_si_el_resto_es_cero_no_queda_un_renglon_vacio():
    """Un lote que entra con el resto del stock quieto: dos renglones."""
    movs = _movs()
    movs[4]["aporte"] = 75812.12               # sólo el hilado del lote
    movs[4]["etiqueta"] = "entró a hil."
    out = _resumen(movs, d_utilidad=round(75812.12 + 8211.37 - 82829.20, 2))
    assert [g["texto"] for g in out] == ["AC 40 · entró la mercadería de 4 anticipos",
                                         "revaluó el stock de hil."]


def test_el_resto_dice_lo_que_hizo_sin_el_lote():
    """Si además de la salida hubo hilado que se tejió, el texto lo dice con
    los kilos netos del lote: 'hil. → tej.', no 'entró a hil.'."""
    fotos = dict(FOTO_1102, hilado_kg=FOTO_1056["hilado_kg"] + 24494.24 - 500,
                 tejido_kg=FOTO_1056["tejido_kg"] + 480)
    stock = t.stock_de_la_ventana(fotos, FOTO_1056)
    assert stock["dkg"]["hilado"] == pytest.approx(24494.24 - 500)
    out = _resumen(stock=stock)
    assert out[1]["texto"] == "hil. → tej. · salió de term."


def test_la_foto_de_la_traza_pasa_el_stock_al_resumen():
    """`una()` lee las dos fotos y se lo pasa a `resumir`; sin eso la regla
    no se entera de que hay lote y vuelve a absorber todo."""
    fila = dict(FOTO_1102, creado_en="2026-09-18T16:02:44", utilidad=491634.76)
    ant = dict(FOTO_1056, creado_en="2026-09-18T15:56:34", utilidad=494655.39)
    with patch.object(t.db, "fetch_all", return_value=[fila, ant]), \
            patch.object(t, "movimientos", return_value=_movs()), \
            patch.object(t, "_desde_cuando_hay_detalle", return_value=1), \
            patch.object(t, "resumir", wraps=t.resumir) as res, \
            patch("modules.informes.eventos.de_la_ventana", return_value=[]), \
            patch("modules.informes.eventos.transacciones", return_value=[]), \
            patch("modules.informes.eventos.indice", return_value=_EV):
        f = t.una(11823)
    assert res.call_args.kwargs["stock"]["lote_kg"] == pytest.approx(24494.24)
    assert [g["texto"] for g in f["resumen"]][0] == "AC 40 · entró la mercadería de 4 anticipos"
    assert len(f["resumen"]) == 3


def test_la_nota_del_dia_pasa_el_mismo_stock():
    from modules.informes import dia
    filas = [FOTO_1056, FOTO_1102]
    with patch.object(dia, "_rows", return_value=filas):
        _causa, stock = dia._fotos_del_dia({"id_traza": 11822}, {"id_traza": 11823})
    assert stock["lote_kg"] == pytest.approx(24494.24)
