"""Foto diaria de la cartera por cliente (30/09/2026).

La historia de la deuda de cada cliente no se reconstruye después: si la
foto no se toma el día que pasa, se pierde. La toma el ciclo de fondo del
servidor, no un script a mano.
"""
import inspect

from modules._lib import autocarga_facturas
from modules.cartera import foto_diaria, queries


def test_toca_sin_foto_de_hoy():
    assert foto_diaria.toca(None) is True


def test_toca_si_la_foto_tiene_mas_de_una_hora():
    assert foto_diaria.toca(61) is True
    assert foto_diaria.toca(60) is True


def test_no_toca_si_la_foto_es_reciente():
    assert foto_diaria.toca(5) is False
    assert foto_diaria.toca(59.9) is False


def test_correr_si_toca_toma_la_foto(monkeypatch):
    llamadas = []
    monkeypatch.setattr(foto_diaria.db, "fetch_one", lambda *a, **k: {"minutos": None})
    monkeypatch.setattr(queries, "tomar_snapshot",
                        lambda f: llamadas.append(f) or {"fecha": str(f), "n_clientes": 3})
    r = foto_diaria.correr_si_toca()
    assert r["corrio"] is True and len(llamadas) == 1


def test_correr_si_toca_respeta_la_foto_reciente(monkeypatch):
    monkeypatch.setattr(foto_diaria.db, "fetch_one", lambda *a, **k: {"minutos": 10})
    monkeypatch.setattr(queries, "tomar_snapshot",
                        lambda f: (_ for _ in ()).throw(AssertionError("no debía correr")))
    assert foto_diaria.correr_si_toca() == {"corrio": False, "motivo": "reciente"}


def test_se_apaga_con_la_variable(monkeypatch):
    monkeypatch.setenv("CARTERA_FOTO_AUTO", "0")
    assert foto_diaria.correr_si_toca()["motivo"] == "apagada"


def test_el_ciclo_de_fondo_la_llama():
    assert "foto_diaria" in inspect.getsource(autocarga_facturas._loop)


def test_la_foto_guarda_cheques_y_antiguedad_y_se_regraba():
    src = inspect.getsource(queries.tomar_snapshot)
    for pedazo in ("scintela.cheque", "FULL JOIN", "cheques_por_cobrar",
                   "cheques_rebotados", "dias_factura_mas_antigua",
                   "saldo_mas_90_dias", "DELETE FROM scintela.cartera_snapshots"):
        assert pedazo in src, pedazo


def test_la_migracion_agrega_las_columnas():
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "0255_foto_diaria_cartera_por_cliente.sql").read_text()
    for col in ("cheques_por_cobrar", "cheques_rebotados",
                "dias_factura_mas_antigua", "saldo_mas_90_dias"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql
