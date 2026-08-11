"""A las 17:00 EC, lo que salió hoy y no tiene factura (TMT 2026-08-11).

Dueña: *"hace al final del día si todavía no fueron facturados"* → *"hacelo
5pm, así les das tiempo de cargar"*.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from modules.asinfo import despacho_sin_factura as ds

CASOS = [
    {"numero": "DES-000095134", "kg": 987.8, "nota": "TONO CEN S/M ADJUNTO FOTO",
     "creado": "10:07", "horas": 5.5},
    {"numero": "DES-000095133", "kg": 47.6, "nota": "", "creado": "09:41",
     "horas": 6.0},
]


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.delenv("DESPACHO_SIN_FACTURA", raising=False)
    monkeypatch.delenv("DESPACHO_SIN_FACTURA_HORA", raising=False)


def _a_las(h):
    """Lo que devuelve `_ahora_ec()`: el reloj de Ecuador, no el del server."""
    return datetime(2026, 8, 11, h, 30, tzinfo=UTC)


def _correr(casos, hora_ec=17):
    puestos = []
    with patch.object(ds, "_ahora_ec", return_value=_a_las(hora_ec)), \
         patch.object(ds, "sin_factura_de_hoy", return_value=casos), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        res = ds.correr_si_toca()
    return res, puestos


def test_a_las_17_avisa_con_los_kilos_adelante():
    res, puestos = _correr(CASOS)
    assert res["avisado"] is True
    a = puestos[0]
    assert a["titulo"] == "2 despachos salieron hoy sin factura · 1.035 kg"
    assert a["clave"] == "desp-sin-factura:2026-08-11"
    assert a["nivel"] == "alerta"


def test_uno_solo_va_en_singular():
    _, puestos = _correr(CASOS[:1])
    assert puestos[0]["titulo"] == "Un despacho salió hoy sin factura · 988 kg"


def test_el_detalle_lista_cada_despacho_con_hora_numero_y_glosa():
    """Sin el número no se puede ir a buscarlo; sin la glosa no se sabe cuál es."""
    _, puestos = _correr(CASOS)
    d = puestos[0]["detalle"]
    assert d == ("10:07 · DES-000095134 · 987,8 kg · TONO CEN S/M ADJUNTO FOTO\n"
                 "09:41 · DES-000095133 · 47,6 kg")


def test_antes_de_las_17_no_dice_nada():
    """A las 16 todavía están cargando: avisar ahí es el ⚠ que se ignora."""
    res, puestos = _correr(CASOS, hora_ec=16)
    assert res["avisado"] is False
    assert res["motivo"] == "todavía no son las 17"
    assert puestos == []


def test_mas_tarde_sigue_avisando_pero_la_clave_lo_deja_entrar_una_vez():
    res, _ = _correr(CASOS, hora_ec=21)
    assert res["avisado"] is True


def test_si_se_facturo_todo_no_enciende_la_campanita():
    res, puestos = _correr([])
    assert res["avisado"] is False
    assert res["motivo"] == "se facturó todo"
    assert puestos == []


def test_apagado_por_env():
    import os
    os.environ["DESPACHO_SIN_FACTURA"] = "0"
    try:
        res, puestos = _correr(CASOS)
    finally:
        del os.environ["DESPACHO_SIN_FACTURA"]
    assert res["motivo"] == "apagado"
    assert puestos == []


# ── la consulta ────────────────────────────────────────────────────────────

def _sql():
    visto = {}

    def _fake(db_id, sql, *a, **k):
        visto["db"], visto["sql"] = db_id, sql
        return []

    with patch("modules._lib.metabase_client.fetch_dataset", side_effect=_fake):
        ds.sin_factura_de_hoy()
    return visto


def test_los_anulados_quedan_afuera_por_definicion():
    """28 de los 32 'sin factura' de agosto estaban ANULADOS: volvieron a
    bodega y no les corresponde factura. Contarlos convierte la operación
    normal de un depósito en una alarma."""
    assert "dc.fecha_anulacion IS NULL" in _sql()["sql"]


def test_solo_los_de_hoy_y_contra_asinfo():
    v = _sql()
    assert v["db"] == 2
    assert "dc.fecha_creacion >= CAST(GETDATE() AS date)" in v["sql"]
    assert "dc.indicador_generado_factura = 0" in v["sql"]


def test_un_despacho_sin_kilos_no_se_lista():
    with patch("modules._lib.metabase_client.fetch_dataset",
               return_value=[{"numero": "DES-1", "kg": 0, "nota": "", "creado": "",
                              "minutos": 60}]):
        assert ds.sin_factura_de_hoy() == []
