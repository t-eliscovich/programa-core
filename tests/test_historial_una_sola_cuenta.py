"""Historial no hace dos veces la misma cuenta.

TMT 2026-08-26 (dueña): *"historia también carga"*, sobre
https://programa.intela.com.ec/informes/historico-12m

Entrar a Historial saca una foto fresca del mes en curso — es a propósito: la
foto tiene que ser igual a lo que muestra Resultados en ese instante. Lo que no
era a propósito es cuánto costaba esa foto. Medido en el laboratorio local, con
la consulta a formulas_app cobrando 500 ms (el peaje de cruzar a la otra base):

    antes                      1.071 ms  ·  2 consultas a formulas
    sin la cuenta repetida       560 ms  ·  1
    y con la caché prendida       24 ms  ·  0

Tres cosas distintas, todas del mismo request:

 1. `ejecutar()` YA calcula los KPIs —el balance entero— antes de mirar si la
    foto del mes existía. Cuando existía (o sea: siempre, del segundo día del
    mes en adelante) se los tiraba y se volvían a calcular.
 2. El "Stock Quí." preguntaba a formulas_app el colorante Y el total, y usaba
    el colorante sólo si el total no contestaba. O sea que la primera consulta
    se tiraba casi siempre.
 3. El total se pregunta dos veces por foto —una para la pantalla y otra para
    el guard que decide si la foto se puede guardar—: es el MISMO número del
    MISMO día.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import snapshot_historia_mensual as snap  # noqa: E402

from modules.informes import queries  # noqa: E402
from modules.informes import quimico_inv_formulas as qif  # noqa: E402

# --- 1. la foto se calcula UNA vez -----------------------------------------

def _sin_throttle(monkeypatch):
    """No hay foto previa reciente → el throttle no frena nada."""
    monkeypatch.setattr(queries.db, "fetch_one", lambda *a, **k: None)


def test_la_foto_no_se_calcula_dos_veces(monkeypatch):
    """`ejecutar` ya los calculó: pedirlos de nuevo es hacer el balance dos veces."""
    _sin_throttle(monkeypatch)
    kpis = {"patrimonio": 20_000_000, "_quimico_fisico": True}
    monkeypatch.setattr(snap, "ejecutar",
                        lambda *a, **k: {"accion": "skipped", "id_historia": 7,
                                         "kpis": kpis})
    monkeypatch.setattr(snap, "insertar_snapshot", lambda k, usuario="": 99)
    with patch.object(snap, "calcular_kpis") as m:
        r = queries.tomar_snapshot_mes_actual(usuario="test", throttle_segundos=0)
    assert m.call_count == 0, "el balance se calculó una segunda vez"
    assert r == {"accion": "inserted", "id_historia": 99, "kpis": kpis}


def test_si_ejecutar_no_trajo_los_kpis_se_calculan(monkeypatch):
    """La red: sin KPIs adentro, la foto se saca igual (no se guarda una vacía)."""
    _sin_throttle(monkeypatch)
    monkeypatch.setattr(snap, "ejecutar",
                        lambda *a, **k: {"accion": "skipped", "id_historia": 7})
    monkeypatch.setattr(snap, "insertar_snapshot", lambda k, usuario="": 99)
    with patch.object(snap, "calcular_kpis",
                      return_value={"patrimonio": 1}) as m:
        r = queries.tomar_snapshot_mes_actual(usuario="test", throttle_segundos=0)
    assert m.call_count == 1
    assert r["kpis"] == {"patrimonio": 1}


# --- 2. el colorante sólo cuando hace falta --------------------------------

_TOTAL = "modules.informes.quimico_inv_formulas.quimico_total_fisico"
_COL = "modules.tintura.service.stock_colorante_fisico"


def _vqx(monkeypatch, total, ultimo_bueno=None):
    """Corre el bloque de químicos del balance y dice a quién le preguntó."""
    monkeypatch.setattr(queries, "_VQX_ULTIMO_BUENO", ultimo_bueno)
    with patch(_TOTAL, return_value=total) as m_tot, \
         patch(_COL, return_value=338_614.0) as m_col:
        vqx = queries._vqx_de_los_quimicos(0.0)
    return vqx, m_tot.call_count, m_col.call_count


def test_si_el_total_contesta_no_se_pregunta_el_colorante(monkeypatch):
    """El caso normal: una consulta a la otra base, no dos."""
    vqx, n_tot, n_col = _vqx(monkeypatch, total=411_000.0)
    assert vqx["vqx"] == 411_000.0
    assert (n_tot, n_col) == (1, 0)


def test_sin_total_pero_con_un_valor_bueno_guardado_tampoco(monkeypatch):
    """Ese último valor bueno le gana igual al colorante: preguntarlo era al pedo."""
    vqx, n_tot, n_col = _vqx(monkeypatch, total=None, ultimo_bueno=409_000.0)
    assert vqx["vqx"] == 409_000.0
    assert vqx["aviso"]
    assert (n_tot, n_col) == (1, 0)


def test_sin_total_y_sin_nada_guardado_se_baja_al_colorante(monkeypatch):
    """App recién arrancada y formulas mudo: recién ahí el colorante se muestra."""
    vqx, n_tot, n_col = _vqx(monkeypatch, total=None)
    assert vqx["vqx"] == 338_614.0
    assert (n_tot, n_col) == (1, 1)


def test_si_el_total_explota_el_balance_sigue(monkeypatch):
    monkeypatch.setattr(queries, "_VQX_ULTIMO_BUENO", None)
    with patch(_TOTAL, side_effect=RuntimeError("formulas caído")), \
         patch(_COL, return_value=338_614.0):
        assert queries._vqx_de_los_quimicos(0.0)["vqx"] == 338_614.0


# --- 3. el mismo número del mismo día se pregunta una sola vez --------------

def test_el_quimico_del_dia_se_pregunta_una_sola_vez(monkeypatch):
    """La pantalla y el guard de la foto piden LO MISMO. La segunda es gratis."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)  # la caché duerme bajo pytest
    qif.reset_quimico_cache()
    filas = [{"num": 101, "num_visible": 1, "familia": "poli", "nombre": "AZUL",
              "us": 10.0, "conteo_f": "1", "conteo_q": 100.0, "cons_q": 0.0,
              "comp_q": 0.0, "aju_q": 0.0}]
    with patch.object(qif.formulas_db, "fetch_all", return_value=filas) as m:
        uno = qif.quimico_final_por_tipo(date(2026, 8, 26))
        dos = qif.quimico_final_por_tipo(date(2026, 8, 26))
    assert m.call_count == 1
    assert uno == dos and uno["total"] > 0
    qif.reset_quimico_cache()


def test_el_dia_siguiente_es_otra_pregunta(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    qif.reset_quimico_cache()
    filas = [{"num": 101, "num_visible": 1, "familia": "poli", "nombre": "AZUL",
              "us": 10.0, "conteo_f": "1", "conteo_q": 100.0, "cons_q": 0.0,
              "comp_q": 0.0, "aju_q": 0.0}]
    with patch.object(qif.formulas_db, "fetch_all", return_value=filas) as m:
        qif.quimico_final_por_tipo(date(2026, 8, 26))
        qif.quimico_final_por_tipo(date(2026, 8, 27))
    assert m.call_count == 2
    qif.reset_quimico_cache()


def test_el_puente_mudo_no_se_cachea(monkeypatch):
    """Un "no pude preguntar" guardado es una mentira que dura toda la ventana."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    qif.reset_quimico_cache()
    with patch.object(qif.formulas_db, "fetch_all", return_value=[]) as m:
        assert qif.quimico_final_por_tipo(date(2026, 8, 26)) is None
        assert qif.quimico_final_por_tipo(date(2026, 8, 26)) is None
    assert m.call_count == 2
    qif.reset_quimico_cache()


def test_bajo_pytest_la_cache_duerme():
    """Un test que mockea el puente tiene que ver SU mock, no lo de antes."""
    assert os.environ.get("PYTEST_CURRENT_TEST")
    filas = [{"num": 101, "num_visible": 1, "familia": "poli", "nombre": "AZUL",
              "us": 10.0, "conteo_f": "1", "conteo_q": 100.0, "cons_q": 0.0,
              "comp_q": 0.0, "aju_q": 0.0}]
    with patch.object(qif.formulas_db, "fetch_all", return_value=filas) as m:
        qif.quimico_final_por_tipo(date(2026, 8, 26))
        qif.quimico_final_por_tipo(date(2026, 8, 26))
    assert m.call_count == 2


def test_el_calentador_deja_el_quimico_del_dia_listo():
    import inspect

    from modules._lib import warmup
    assert "quimico_total_fisico" in inspect.getsource(warmup._warm_once)
