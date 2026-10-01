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


def test_detecta_y_suma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=(FILAS, True)):
        r = sss.detectar(10)
    assert r["ok"] and len(r["salidas"]) == 2
    assert r["lotes_sin_bajar"] == 167 and r["kg_sin_bajar"] == 4509
    assert r["salidas"][0]["bodega"] == "Hilo"


def test_alerta_y_un_aviso_por_salida():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=(FILAS, True)), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert h["ok"] is False and h["alerts"][0]["category"] == "salidas_sin_bajar_saldo"
    assert [a["clave"] for a in avisos] == ["salida-sin-saldo:SM-000112721", "salida-sin-saldo:SM-000112399"]
    assert "108 de 109 lotes no bajaron" in avisos[0]["titulo"]
    assert "también están en otra salida" in avisos[1]["titulo"]


def test_sin_problemas_ok():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=([], True)):
        h = sss.health(10)
    assert h["ok"] is True and h["alerts"] == []


def test_asinfo_no_contesta_no_alarma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=([], False)):
        h = sss.health(10)
    assert h["ok"] is True and h["stats"]["sin_datos"] is True


def test_la_consulta_compara_el_saldo_contra_la_fecha_de_la_salida():
    q = sss._sql(10)
    assert "s.sf < CAST(sal.fecha_creacion AS date)" in q
    assert "WHERE b = 51" in q          # repetidos sólo en hilo
    assert "indicador_anulacion" in q


def test_health_all_lo_incluye():
    import inspect

    from modules.admin_dbase import health_audit_view as hav
    src = inspect.getsource(hav.health_all)
    assert "salidas_sin_saldo()" in src and 'data29["ok"]' in src
