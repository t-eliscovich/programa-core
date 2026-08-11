"""El placeholder que sostiene el hilo despachado sin orden (TMT 2026-08-11).

Dueña: *"necesito armar el placeholder de esperando orden, y que en el día no
se baje la utilidad. al final del día si no fue cargada manda nuevamente aviso
y ahí sí proceder a bajarla"*.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from modules.asinfo import hilo_sin_of as hs


def _caso(bodega=51, kg=4860.0, num="OSM-000010458"):
    return {"numero": num, "id_bodega": bodega,
            "material": "hilo" if bodega == 51 else "tela cruda",
            "kg": kg, "usuario": "mprima", "descripcion": "", "creado": ""}


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.delenv("HILO_PLACEHOLDER", raising=False)
    monkeypatch.delenv("HILO_PLACEHOLDER_CORTE", raising=False)


def _kg(hora_ec, casos, encendido=True, monkeypatch=None):
    if encendido:
        monkeypatch.setenv("HILO_PLACEHOLDER", "1")
    with patch.object(hs, "despachos_sin_of", return_value=casos), \
         patch.object(hs, "_ahora_ec",
                      return_value=datetime(2026, 8, 11, hora_ec, 0, tzinfo=UTC)):
        return hs.esperando_orden_kg()


def test_apagado_por_defecto_no_sostiene_nada(monkeypatch):
    """Se deploya OSCURO: mueve la utilidad, se enciende después del dry-run."""
    assert hs.placeholder_activo() is False
    assert _kg(10, [_caso()], encendido=False, monkeypatch=monkeypatch) == {}


def test_durante_el_dia_sostiene_los_kilos(monkeypatch):
    assert _kg(10, [_caso(), _caso(kg=3375.0, num="OSM-000010462")],
               monkeypatch=monkeypatch) == {51: 8235.0}


def test_a_las_18_el_corte_los_da_de_baja(monkeypatch):
    """El aviso de las 17 ya salió: la baja llega avisada y en horario."""
    assert _kg(18, [_caso()], monkeypatch=monkeypatch) == {}
    assert _kg(21, [_caso()], monkeypatch=monkeypatch) == {}


def test_a_las_17_todavia_sostiene(monkeypatch):
    """A las 17 sale el aviso, no la baja. Es la hora de arreglarlo."""
    assert _kg(17, [_caso()], monkeypatch=monkeypatch) == {51: 4860.0}


def test_separa_hilo_de_tela_cruda(monkeypatch):
    assert _kg(10, [_caso(), _caso(bodega=52, kg=400.0, num="OSTC-1")],
               monkeypatch=monkeypatch) == {51: 4860.0, 52: 400.0}


def test_solo_los_despachos_de_HOY(monkeypatch):
    """Los de enero y junio no entran: 'de hoy para adelante'."""
    monkeypatch.setenv("HILO_PLACEHOLDER", "1")
    visto = {}
    with patch.object(hs, "despachos_sin_of",
                      side_effect=lambda **k: visto.update(k) or []), \
         patch.object(hs, "_ahora_ec",
                      return_value=datetime(2026, 8, 11, 10, 0, tzinfo=UTC)):
        hs.esperando_orden_kg()
    assert visto == {"dias": 0}      # 0 = hoy; 1 arrancaría AYER a las 00:00


def test_el_inventario_suma_el_placeholder_al_hilo():
    """hilado = bodega 51 + en máquinas + esperando orden."""
    from modules.asinfo import service as svc
    with patch.object(svc, "stock_asinfo_lote_totales",
                      return_value=[{"id_bodega": 51, "total_kg": 1_987_666.0},
                                    {"id_bodega": 52, "total_kg": 250_194.0},
                                    {"id_bodega": 53, "total_kg": 348_402.0}]), \
         patch.object(svc, "fabricacion_proceso",
                      side_effect=lambda b: {"resumen": {"saldo": 31_164.0 if b == 52 else 47_966.0}}), \
         patch.object(svc, "disponible", return_value=True), \
         patch.object(svc, "_cache_ok", return_value=True), \
         patch("modules.asinfo.hilo_sin_of.esperando_orden_kg",
               return_value={51: 12_292.0}):
        svc._INVENTARIO_ETAPA_CACHE.clear()
        inv = svc.inventario_por_etapa()
        svc._INVENTARIO_ETAPA_CACHE.clear()
    assert inv["esperando_orden_hilo"] == 12_292.0
    assert inv["hilo_total"] == 1_987_666.0 + 31_164.0 + 12_292.0
    # y sin placeholder el número es el de siempre
    assert inv["cruda_total"] == 250_194.0 + 47_966.0
