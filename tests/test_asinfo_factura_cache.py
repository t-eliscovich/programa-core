"""La caché del detalle de facturas — la que sobrevive al deploy.

TMT 2026-08-25 (dueña): *"el que se llevó carga lento"*. Medido: 630-780 ms, y
la pregunta más tonta posible contra Asinfo tarda lo mismo. El peaje es del
puente, así que la única salida es no volver a preguntar.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.asinfo import factura_lineas as fl

NUM = "001-099-000182419"


@pytest.fixture(autouse=True)
def _limpiar():
    fl.reset_cache()
    yield
    fl.reset_cache()


def _fila(tela="Jersey 3", color="MARINO", kg=22.45, precio=9.82, neto=180.12,
          numero=NUM):
    bruto = round(kg * precio, 4)
    return {"numero": numero, "tela": tela, "codigo": color[:3],
            "producto": f"{tela} {color}", "categoria": "TELAS", "color": color,
            "calidad": "PRIMERA", "cantidad": kg, "precio": precio,
            "bruto": bruto, "descuento": round(bruto - neto, 4),
            "pct1": 5, "pct2": 14}


# --- leer ------------------------------------------------------------------

def test_si_esta_en_la_base_no_se_le_pregunta_a_asinfo():
    guardado = {"estado": "ok", "lineas": [], "servicios": [], "totales": {"kg": 1}}
    with patch.object(fl, "_de_la_base", return_value=guardado), \
         patch("modules._lib.metabase_client.disponible") as disp:
        res = fl.que_se_llevo(NUM)
    assert res is guardado
    disp.assert_not_called()  # ni se toca el puente


def test_lo_de_la_base_queda_tambien_en_memoria():
    guardado = {"estado": "ok", "lineas": [], "servicios": [], "totales": {}}
    with patch.object(fl, "_de_la_base", return_value=guardado) as base:
        fl.que_se_llevo(NUM)
        fl.que_se_llevo(NUM)
    assert base.call_count == 1  # la segunda ni va a Postgres


def test_sin_base_se_le_pregunta_a_asinfo_igual():
    """La caché no es una fuente: si Postgres no contesta, se pregunta."""
    with patch.object(fl, "_de_la_base", return_value=None), \
         patch.object(fl, "_guardar"), \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([_fila()], True)) as m:
        res = fl.que_se_llevo(NUM)
    assert res["estado"] == "ok"
    m.assert_called_once()


# --- escribir --------------------------------------------------------------

def test_lo_que_trae_asinfo_se_guarda():
    with patch.object(fl, "_de_la_base", return_value=None), \
         patch.object(fl, "_guardar") as guardar, \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([_fila()], True)):
        fl.que_se_llevo(NUM)
    guardar.assert_called_once()
    assert guardar.call_args[0][0] == NUM


def test_un_fracaso_no_se_guarda_nunca():
    """Un 'no pude preguntar' guardado es una mentira que dura para siempre."""
    with patch("db.execute") as escribir:
        fl._guardar(NUM, {"estado": "error", "lineas": []})
    escribir.assert_not_called()


def test_la_base_no_devuelve_un_error_guardado():
    """Aunque alguien meta basura en la tabla, sólo se cree lo que dice ok."""
    fila = {"datos": {"estado": "error", "lineas": []}}
    with patch("db.fetch_one", return_value=fila):
        assert fl._de_la_base(NUM) is None


def test_la_base_caida_no_rompe_la_pantalla():
    with patch("db.fetch_one", side_effect=RuntimeError("Postgres no está")):
        assert fl._de_la_base(NUM) is None


# --- la precarga -----------------------------------------------------------

def test_la_precarga_trae_el_dia_entero_en_una_consulta():
    filas = [_fila(numero="001-099-000000001"),
             _fila(numero="001-099-000000001", tela="Rib"),
             _fila(numero="001-099-000000002")]
    with patch.object(fl, "_guardar") as guardar, \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(filas, True)) as m:
        n = fl.precargar(dias=3, cada_secs=0)
    assert n == 2                      # dos facturas, una sola consulta
    assert m.call_count == 1
    assert guardar.call_count == 2


def test_la_precarga_no_corre_dos_veces_seguidas():
    """Si corriera en cada vuelta del calentador, serían 20.000 filas por minuto."""
    with patch.object(fl, "_guardar"), \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([_fila()], True)) as m:
        fl.precargar(dias=3)
        fl.precargar(dias=3)
    assert m.call_count == 1


def test_la_precarga_saltea_los_numeros_que_no_son_del_sri():
    with patch.object(fl, "_guardar") as guardar, \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([_fila(numero="")], True)):
        assert fl.precargar(dias=1, cada_secs=0) == 0
    guardar.assert_not_called()


def test_la_precarga_sin_puente_no_hace_nada():
    with patch("modules._lib.metabase_client.disponible", return_value=False):
        assert fl.precargar(dias=1, cada_secs=0) == 0


def test_el_dia_se_pide_por_fecha_de_factura():
    sql = fl._sql_where("fc.fecha >= '2026-08-22'")
    assert "fc.fecha >= '2026-08-22'" in sql
    assert "fc.numero" in sql          # hace falta para agrupar
