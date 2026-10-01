"""Salidas de material que Asinfo no bajó del saldo (Tamara 2026-10-01)."""
from __future__ import annotations

from unittest.mock import patch

from modules.asinfo import salidas_sin_saldo as sss

FILAS = [
    {"doc": "SM-000112721", "bodega": 51, "orden": "OSM-000010962",
     "fecha": "2026-09-23 14:48", "lotes": 109, "sin_bajar": 108,
     "kg_sin_bajar": 2916, "repetidos": 0},
    {"doc": "SM-000112399", "bodega": 51, "orden": "OSM-000010926",
     "fecha": "2026-09-19 06:18", "lotes": 60, "sin_bajar": 59,
     "kg_sin_bajar": 1593, "repetidos": 59},
]


ING = [
    {"doc": "BOD-000002371/IM-0000591", "bodega": 51, "fecha": "2026-09-11 15:50",
     "lotes": 29, "kg_de_mas": 789.38},
]


CUADRA = [
    {"bodega": 51, "saldo": 1000, "movimientos": 1000 - 2300, "diferencia": 2300},
    {"bodega": 52, "saldo": 500, "movimientos": 500, "diferencia": 0},
]


def _fake(salidas=FILAS, ingresos=(), ok=True, cuadre=CUADRA):
    def f(db, sql, **k):
        if "AS diferencia" in sql:
            return list(cuadre), ok
        return (list(ingresos) if "operacion = 1" in sql else list(salidas)), ok
    return f


def test_detecta_y_suma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=(FILAS, True)):
        r = sss.detectar(10)
    assert r["ok"] and len(r["salidas"]) == 2
    assert r["lotes_sin_bajar"] == 167 and r["kg_sin_bajar"] == 4509
    assert r["salidas"][0]["bodega"] == "Hilo"


def test_alerta_y_un_aviso_por_salida():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=_fake()), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert h["ok"] is False and h["alerts"][0]["category"] == "salidas_sin_bajar_saldo"
    assert [a["clave"] for a in avisos] == ["salida-sin-saldo:SM-000112721", "salida-sin-saldo:SM-000112399"]
    assert "108 de 109 lotes no bajaron" in avisos[0]["titulo"]
    assert "también están en otra salida" in avisos[1]["titulo"]


def test_sin_problemas_ok():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=_fake([], [])):
        h = sss.health(10)
    assert h["ok"] is True and h["alerts"] == []


def test_asinfo_no_contesta_no_alarma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=([], False)):
        h = sss.health(10)
    assert h["ok"] is True and h["stats"]["sin_datos"] is True


def test_la_consulta_compara_el_saldo_contra_la_fecha_de_la_salida():
    q = sss._sql(10)
    assert "s.saldo - k.kx > 0.5" in q           # saldo contra movimientos
    assert "u.rn = 1" in q                        # se le anota a la última salida
    assert "WHERE b = 51" in q          # repetidos sólo en hilo
    assert "indicador_anulacion" in q


def test_health_all_lo_incluye():
    import inspect

    from modules.admin_dbase import health_audit_view as hav
    src = inspect.getsource(hav.health_all)
    assert "salidas_sin_saldo()" in src and 'data29["ok"]' in src


def test_ingreso_sumado_dos_veces_alerta_y_avisa():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake([], ING)), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert h["ok"] is False
    assert [a["category"] for a in h["alerts"]] == ["ingresos_sumados_dos_veces"]
    assert h["stats"]["kg_ingreso_de_mas"] == 789.38
    assert [a["clave"] for a in avisos] == ["ingreso-doble:BOD-000002371/IM-0000591"]
    assert "29 lotes" in avisos[0]["titulo"] and "Hilo" in avisos[0]["titulo"]


def test_las_dos_alertas_juntas():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake(FILAS, ING)), \
         patch("modules.avisos.queries.avisar", return_value=True):
        h = sss.health(10)
    assert {a["category"] for a in h["alerts"]} == {
        "salidas_sin_bajar_saldo", "ingresos_sumados_dos_veces"}


def test_detectar_ingresos_falla_sin_levantar():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        r = sss.detectar_ingresos(30)
    assert r["ok"] is False and r["ingresos"] == []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([{"doc": "X", "bodega": "no"}], True)):
        assert sss.detectar_ingresos(30)["ingresos"] == []


def test_la_consulta_de_ingresos_compara_saldo_contra_movimientos():
    q = sss._sql_ingresos(30)
    assert "k.nsal = 0" in q and "s.saldo - k.kx > 0.5" in q
    assert "ip.operacion = 1" in q and "indicador_anulacion" in q


def test_si_solo_contesta_una_consulta_igual_alerta():
    def f(db, sql, **k):
        if "operacion = 1" in sql:
            return list(ING), True
        if "AS diferencia" in sql:
            return list(CUADRA), True
        return [], False
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=f), \
         patch("modules.avisos.queries.avisar", side_effect=RuntimeError("sin base")):
        h = sss.health(10)
    assert [a["category"] for a in h["alerts"]] == ["ingresos_sumados_dos_veces"]
    assert h["stats"]["salidas"] == []


DESCUADRE = [
    {"bodega": 51, "saldo": 2202034, "movimientos": 2144885, "diferencia": 57149},
    {"bodega": 52, "saldo": 254354, "movimientos": 206950, "diferencia": 47404},
    {"bodega": 53, "saldo": 318328, "movimientos": 318298, "diferencia": 30},
]


def test_cuadre_marca_lo_que_pasa_de_la_base():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(DESCUADRE + [{"bodega": "x"}], True)):
        c = sss.cuadre()
    b = {x["id"]: x for x in c["bodegas"]}
    assert b[51]["descuadrada"] and b[51]["de_mas_nuevo"] == 57149 - 2300
    assert b[52]["descuadrada"] and b[53]["descuadrada"] is False


def test_cuadre_falla_sin_levantar():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        assert sss.cuadre()["ok"] is False
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        assert sss.cuadre()["ok"] is False


def test_bodega_descuadrada_alerta_y_avisa_cada_mil_kg():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake([], [], cuadre=DESCUADRE)), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert [a["category"] for a in h["alerts"]] == ["bodega_descuadrada"]
    assert [a["clave"] for a in avisos] == ["cuadre:51:54", "cuadre:52:47"]
    assert "tela cruda no cuadra" in avisos[1]["titulo"]
    assert len(h["stats"]["cuadre"]) == 3


def test_corre_solo_cada_media_hora(monkeypatch):
    llamadas = []
    monkeypatch.setattr(sss, "health", lambda **k: llamadas.append(k) or {
        "ok": False, "alerts": [{"category": "bodega_descuadrada"}]})
    monkeypatch.setattr(sss, "_auto_ultimo", None)
    monkeypatch.delenv("SALIDAS_SALDO_AUTO", raising=False)
    monkeypatch.setenv("SALIDAS_SALDO_SECS", "nada")
    r = sss.correr_si_toca()
    assert r["corrio"] and r["alertas"] == ["bodega_descuadrada"]
    assert sss.correr_si_toca()["corrio"] is False, "no repite antes de 30 min"
    assert llamadas == [{"avisar": True}]


def test_corre_solo_se_apaga_y_no_se_cae(monkeypatch):
    monkeypatch.setattr(sss, "_auto_ultimo", None)
    monkeypatch.setenv("SALIDAS_SALDO_AUTO", "0")
    assert sss.correr_si_toca()["corrio"] is False
    monkeypatch.setenv("SALIDAS_SALDO_AUTO", "1")
    monkeypatch.setenv("SALIDAS_SALDO_SECS", "600")

    def boom(**k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sss, "health", boom)
    assert sss.correr_si_toca()["corrio"] is False


def test_el_hilo_de_fondo_lo_llama():
    import inspect

    from modules._lib import autocarga_facturas as af
    assert "_sss.correr_si_toca()" in inspect.getsource(af._loop)
