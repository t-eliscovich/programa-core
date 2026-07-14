"""Tests del bridge a formulas_app (modules/tintura).

Cubre:
    - tinturado_resumen(): mapping de rows del DB a TinturadoOrden,
      cálculo de desperdicio_kg, manejo de nulls (orden no terminada).
    - stock_quimicos(): mapping a StockProducto, productos sin lecturas
      llegan con stock_kg=0 y fecha_lectura=None.
    - disponible() refleja formulas_db.disponible().
    - Cero llamadas reales a DB: monkeypatch sobre formulas_db.fetch_all.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.tintura import service
from modules.tintura.service import StockProducto, TinturadoOrden

# ---------------------------------------------------------------------------
# tinturado_resumen
# ---------------------------------------------------------------------------


def test_tinturado_resumen_devuelve_lista_vacia_si_bridge_caido():
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[]):
        assert service.tinturado_resumen() == []


def test_tinturado_resumen_mapea_orden_terminada_con_desperdicio():
    row = {
        "numero": "24089",
        "fecha": "15/04/2026",
        "fecha_terminado": "2026-04-17",
        "formula_cod": "AZ-117",
        "color": "Azul Marino",
        "categoria": "Jersey",
        "kilos_planeados": 200.0,
        "tela_cruda_kg": 205.5,
        "tela_terminada_kg": 198.7,
        "jet": 3,
        "es_reproceso": False,
        "observaciones": None,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        result = service.tinturado_resumen()
    assert len(result) == 1
    o = result[0]
    assert isinstance(o, TinturadoOrden)
    assert o.numero == "24089"
    assert o.fecha == date(2026, 4, 15)
    assert o.fecha_terminado == date(2026, 4, 17)
    assert o.formula_cod == "AZ-117"
    assert o.color == "Azul Marino"
    assert o.tela_cruda_kg == 205.5
    assert o.tela_terminada_kg == 198.7
    assert o.desperdicio_kg == 6.8
    assert o.jet == 3
    assert o.es_reproceso is False


def test_tinturado_resumen_orden_no_terminada_desperdicio_es_none():
    """Orden con tela_terminada_kg=None: desperdicio no se puede calcular."""
    row = {
        "numero": "24112",
        "fecha": "16/04/2026",
        "fecha_terminado": None,
        "formula_cod": "RJ-50",
        "color": "Rojo",
        "categoria": "Pique",
        "kilos_planeados": 100,
        "tela_cruda_kg": 102.0,
        "tela_terminada_kg": None,
        "jet": 2,
        "es_reproceso": False,
        "observaciones": "todavía en el jet",
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        o = service.tinturado_resumen()[0]
    assert o.desperdicio_kg is None
    assert o.fecha_terminado is None
    assert o.observaciones == "todavía en el jet"


def test_tinturado_resumen_ambos_kg_none_desperdicio_none():
    row = {
        "numero": "24113",
        "fecha": "16/04/2026",
        "fecha_terminado": None,
        "formula_cod": None,
        "color": None,
        "categoria": None,
        "kilos_planeados": None,
        "tela_cruda_kg": None,
        "tela_terminada_kg": None,
        "jet": None,
        "es_reproceso": False,
        "observaciones": None,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        o = service.tinturado_resumen()[0]
    assert o.desperdicio_kg is None
    assert o.kilos_planeados == 0.0
    assert o.jet is None
    assert o.color is None


def test_tinturado_resumen_es_reproceso_se_normaliza_a_bool():
    """Si la DB devuelve False/None/1/0 — el dataclass siempre tiene bool."""
    for raw, expected in [(False, False), (True, True), (None, False), (1, True), (0, False)]:
        row = {
            "numero": "X",
            "fecha": "01/01/2026",
            "fecha_terminado": None,
            "formula_cod": "F",
            "color": "",
            "categoria": "",
            "kilos_planeados": 0,
            "tela_cruda_kg": None,
            "tela_terminada_kg": None,
            "jet": None,
            "es_reproceso": raw,
            "observaciones": None,
        }
        with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
            o = service.tinturado_resumen()[0]
        assert o.es_reproceso is expected, f"esperaba {expected} para raw={raw!r}"


def test_tinturado_resumen_fecha_invalida_queda_none():
    row = {
        "numero": "X",
        "fecha": "no-es-fecha",
        "fecha_terminado": "tampoco",
        "formula_cod": "F",
        "color": "",
        "categoria": "",
        "kilos_planeados": 0,
        "tela_cruda_kg": None,
        "tela_terminada_kg": None,
        "jet": None,
        "es_reproceso": False,
        "observaciones": None,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        o = service.tinturado_resumen()[0]
    assert o.fecha is None
    assert o.fecha_terminado is None


def test_tintura_parsers_toleran_tipos_y_numeros_invalidos():
    assert service._parse_ddmmyyyy(None) is None
    assert service._parse_iso(date(2026, 5, 10)) == date(2026, 5, 10)
    assert service._f("no-numero", default=7.5) == 7.5
    assert service._fo("no-numero") is None


def test_tinturado_resumen_to_dict_serializa_fechas():
    row = {
        "numero": "X",
        "fecha": "15/04/2026",
        "fecha_terminado": "2026-04-20",
        "formula_cod": "F",
        "color": "",
        "categoria": "",
        "kilos_planeados": 0,
        "tela_cruda_kg": 10,
        "tela_terminada_kg": 9,
        "jet": 1,
        "es_reproceso": False,
        "observaciones": None,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        d = service.tinturado_resumen()[0].to_dict()
    assert d["fecha"] == "2026-04-15"
    assert d["fecha_terminado"] == "2026-04-20"
    assert d["desperdicio_kg"] == 1.0


def test_tinturado_resumen_pasa_limite_a_la_query():
    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.tinturado_resumen(limite=25)
    _, params = m.call_args[0][0], m.call_args[0][1]
    assert params == (25,)


def test_tinturado_resumen_filtra_solo_terminadas():
    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.tinturado_resumen(solo_terminadas=True)
    sql_passed = m.call_args[0][0]
    assert "fecha_terminado IS NOT NULL" in sql_passed


def test_tinturado_resumen_filtra_por_rango_de_creacion():
    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.tinturado_resumen(
            creacion_desde=date(2026, 5, 1),
            creacion_hasta=date(2026, 5, 31),
        )
    sql_passed = m.call_args[0][0]
    params = m.call_args[0][1]
    assert "TO_DATE(o.fecha, 'DD/MM/YYYY') >= %s" in sql_passed
    assert "TO_DATE(o.fecha, 'DD/MM/YYYY') <= %s" in sql_passed
    assert date(2026, 5, 1) in params
    assert date(2026, 5, 31) in params


def test_tinturado_resumen_filtra_por_rango_terminado_implica_solo_terminadas():
    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.tinturado_resumen(
            terminado_desde=date(2026, 5, 1),
            terminado_hasta=date(2026, 5, 31),
        )
    sql_passed = m.call_args[0][0]
    params = m.call_args[0][1]
    assert "fecha_terminado IS NOT NULL" in sql_passed
    assert "fecha_terminado >= %s" in sql_passed
    assert "2026-05-01" in params
    assert "2026-05-31" in params


# ---------------------------------------------------------------------------
# desperdicio_periodo
# ---------------------------------------------------------------------------


def test_desperdicio_periodo_por_terminado_calcula_pct():
    row = {"ordenes_count": 12, "crudo": 1000.0, "terminado": 950.0}
    with patch("modules.tintura.service.formulas_db.fetch_one", return_value=row):
        d = service.desperdicio_periodo(date(2026, 5, 1), date(2026, 5, 31))
    assert d["ordenes_count"] == 12
    assert d["kilos_crudo_total"] == 1000.0
    assert d["kilos_terminado_total"] == 950.0
    assert d["desperdicio_kg_total"] == 50.0
    assert d["desperdicio_pct"] == 5.0


def test_desperdicio_periodo_crudo_cero_pct_es_none():
    row = {"ordenes_count": 0, "crudo": 0, "terminado": 0}
    with patch("modules.tintura.service.formulas_db.fetch_one", return_value=row):
        d = service.desperdicio_periodo(date(2026, 1, 1), date(2026, 1, 31))
    assert d["desperdicio_pct"] is None


def test_desperdicio_periodo_sin_data_devuelve_zeros():
    """formulas_db.fetch_one devuelve None → dict bien formado en 0."""
    with patch("modules.tintura.service.formulas_db.fetch_one", return_value=None):
        d = service.desperdicio_periodo(date(2026, 1, 1), date(2026, 1, 31))
    assert d["ordenes_count"] == 0
    assert d["kilos_crudo_total"] == 0.0
    assert d["desperdicio_kg_total"] == 0.0
    assert d["desperdicio_pct"] is None


def test_desperdicio_periodo_por_terminado_filtra_sobre_fecha_terminado():
    with patch("modules.tintura.service.formulas_db.fetch_one") as m:
        m.return_value = {"ordenes_count": 0, "crudo": 0, "terminado": 0}
        service.desperdicio_periodo(
            date(2026, 5, 1), date(2026, 5, 31), por="terminado"
        )
    sql_passed = m.call_args[0][0]
    params = m.call_args[0][1]
    assert "fecha_terminado IS NOT NULL" in sql_passed
    assert "fecha_terminado >= %s" in sql_passed
    assert params == ("2026-05-01", "2026-05-31")


def test_desperdicio_periodo_por_creacion_filtra_sobre_fecha():
    with patch("modules.tintura.service.formulas_db.fetch_one") as m:
        m.return_value = {"ordenes_count": 0, "crudo": 0, "terminado": 0}
        service.desperdicio_periodo(
            date(2026, 5, 1), date(2026, 5, 31), por="creacion"
        )
    sql_passed = m.call_args[0][0]
    params = m.call_args[0][1]
    assert "TO_DATE(fecha, 'DD/MM/YYYY') >= %s" in sql_passed
    assert params == (date(2026, 5, 1), date(2026, 5, 31))


def test_desperdicio_periodo_por_invalido_levanta_value_error():
    with pytest.raises(ValueError):
        service.desperdicio_periodo(
            date(2026, 1, 1), date(2026, 1, 31), por="otro"
        )


# ---------------------------------------------------------------------------
# stock_quimicos
# ---------------------------------------------------------------------------


def test_stock_quimicos_devuelve_lista_vacia_si_bridge_caido():
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[]):
        assert service.stock_quimicos() == []


def test_stock_quimicos_mapea_row_con_lectura_reciente():
    row = {
        "num": 42,
        "num_visible": 13,
        "familia": "AUX",
        "nombre": "Ácido Acético",
        "unidad": "kg",
        "precio_us": 1.234,
        "stock_kg": 87.5,
        "fecha_lectura": "2026-05-10",
        "nota": "conteo mensual",
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        result = service.stock_quimicos()
    assert len(result) == 1
    s = result[0]
    assert isinstance(s, StockProducto)
    assert s.num == 42
    assert s.num_visible == 13
    assert s.familia == "AUX"
    assert s.nombre == "Ácido Acético"
    assert s.precio_us == 1.234
    assert s.stock_kg == 87.5
    assert s.fecha_lectura == date(2026, 5, 10)
    assert s.nota == "conteo mensual"


def test_stock_quimicos_producto_sin_lectura_queda_en_cero():
    """LEFT JOIN: producto sin entradas en inventario → stock_kg=0, fecha=None."""
    row = {
        "num": 99,
        "num_visible": 7,
        "familia": "COL",
        "nombre": "Colorante X",
        "unidad": "kg",
        "precio_us": 5.0,
        "stock_kg": 0,
        "fecha_lectura": None,
        "nota": "",
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        s = service.stock_quimicos()[0]
    assert s.stock_kg == 0.0
    assert s.fecha_lectura is None


def test_stock_quimicos_to_dict_serializa_fecha():
    row = {
        "num": 1,
        "num_visible": 1,
        "familia": "AUX",
        "nombre": "X",
        "unidad": "kg",
        "precio_us": 1,
        "stock_kg": 10,
        "fecha_lectura": "2026-05-10",
        "nota": "",
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        d = service.stock_quimicos()[0].to_dict()
    assert d["fecha_lectura"] == "2026-05-10"
    assert d["stock_kg"] == 10.0


def test_stock_quimicos_robusto_ante_nulls():
    """Una fila degenerada con nulls/strings no debería romper."""
    row = {
        "num": None,
        "num_visible": None,
        "familia": None,
        "nombre": None,
        "unidad": None,
        "precio_us": "",
        "stock_kg": None,
        "fecha_lectura": "",
        "nota": None,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        s = service.stock_quimicos()[0]
    assert s.num == 0
    assert s.precio_us == 0.0
    assert s.stock_kg == 0.0
    assert s.fecha_lectura is None
    assert s.nota == ""


# ---------------------------------------------------------------------------
# disponible()
# ---------------------------------------------------------------------------


def test_disponible_refleja_formulas_db(monkeypatch):
    from modules._lib import formulas_db

    monkeypatch.setattr(formulas_db, "disponible", lambda: True)
    assert service.disponible() is True
    monkeypatch.setattr(formulas_db, "disponible", lambda: False)
    assert service.disponible() is False


# ---------------------------------------------------------------------------
# stock_quimicos_al_dia
# ---------------------------------------------------------------------------


def test_stock_al_dia_vacio_si_bridge_caido():
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[]):
        assert service.stock_quimicos_al_dia() == []


def test_stock_al_dia_mapea_componentes_correctamente():
    row = {
        "num": 5,
        "num_visible": 3,
        "familia": "AUX",
        "nombre": "Sulfato",
        "unidad": "kg",
        "precio_us": 0.50,
        "lectura_kg": 100.0,
        "fecha_lectura": "2026-05-01",
        "ajustes_kg": -2.0,
        "compras_kg": 50.0,
        "consumo_kg": 30.0,
        "stock_al_dia_kg": 118.0,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        result = service.stock_quimicos_al_dia()
    assert len(result) == 1
    s = result[0]
    assert s.lectura_kg == 100.0
    assert s.ajustes_kg == -2.0
    assert s.compras_kg == 50.0
    assert s.consumo_kg == 30.0
    assert s.stock_al_dia_kg == 118.0
    assert s.fecha_lectura == date(2026, 5, 1)


def test_stock_al_dia_producto_sin_lectura_arranca_de_cero():
    """Si nunca se contó stock, lectura_kg=0 y fecha=None, pero el cálculo
    igual suma compras y resta consumo desde el día 0."""
    row = {
        "num": 7,
        "num_visible": 9,
        "familia": "COL",
        "nombre": "Nuevo",
        "unidad": "kg",
        "precio_us": 2.0,
        "lectura_kg": 0,
        "fecha_lectura": None,
        "ajustes_kg": 0,
        "compras_kg": 25.0,
        "consumo_kg": 5.0,
        "stock_al_dia_kg": 20.0,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        s = service.stock_quimicos_al_dia()[0]
    assert s.fecha_lectura is None
    assert s.lectura_kg == 0.0
    assert s.stock_al_dia_kg == 20.0


def test_stock_al_dia_pasa_fecha_corte_a_la_query():
    """El parámetro fecha se pasa como 'corte' a la query."""
    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.stock_quimicos_al_dia(fecha=date(2026, 6, 15))
    params = m.call_args[0][1]
    assert params == {"corte": "2026-06-15"}


def test_stock_al_dia_default_es_hoy(monkeypatch):
    """Sin fecha explícita, usa date.today()."""
    from datetime import date as _date

    with patch("modules.tintura.service.formulas_db.fetch_all") as m:
        m.return_value = []
        service.stock_quimicos_al_dia()
    params = m.call_args[0][1]
    # No comparamos el valor exacto porque depende del reloj; verificamos shape.
    assert "corte" in params
    # Debe ser un string ISO parseable a date
    _date.fromisoformat(params["corte"])


def test_stock_al_dia_to_dict_serializa_fecha():
    row = {
        "num": 1,
        "num_visible": 1,
        "familia": "AUX",
        "nombre": "X",
        "unidad": "kg",
        "precio_us": 1,
        "lectura_kg": 10,
        "fecha_lectura": "2026-05-01",
        "ajustes_kg": 0,
        "compras_kg": 5,
        "consumo_kg": 2,
        "stock_al_dia_kg": 13,
    }
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[row]):
        d = service.stock_quimicos_al_dia()[0].to_dict()
    assert d["fecha_lectura"] == "2026-05-01"
    assert d["stock_al_dia_kg"] == 13.0


# ---------------------------------------------------------------------------
# costo_por_orden (corte tintura 2026-07-07)
# ---------------------------------------------------------------------------


def test_costo_por_orden_sin_filtros_vacio():
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[]):
        assert service.costo_por_orden() == {}


def test_costo_por_orden_con_todos_los_filtros_y_nulls():
    rows = [
        {"numero": "24089", "costo_us": 32.5},
        {"numero": None, "costo_us": None},  # numero None -> "", costo None -> 0.0
    ]
    with patch(
        "modules.tintura.service.formulas_db.fetch_all", return_value=rows
    ) as m:
        result = service.costo_por_orden(
            creacion_desde=date(2026, 7, 7),
            creacion_hasta=date(2026, 7, 31),
            terminado_desde=date(2026, 7, 7),
            terminado_hasta=date(2026, 7, 31),
        )
    assert result == {"24089": 32.5, "": 0.0}
    # los 4 filtros de fecha se pasaron como params
    _sql, params = m.call_args[0]
    assert len(params) == 4


# ---------------------------------------------------------------------------
# tinto_equiv_formulas (corte tintura 2026-07-07)
# ---------------------------------------------------------------------------


def test_tinto_equiv_formulas_vacio():
    with patch("modules.tintura.service.formulas_db.fetch_all", return_value=[]):
        assert service.tinto_equiv_formulas() == []


def test_tinto_equiv_formulas_mapea_y_excluye_lavados_por_defecto():
    rows = [
        {
            "numero": "24089",
            "fecha": "07/07/2026",
            "fecha_terminado": "2026-07-09",
            "cod": "AZ-117",
            "color": "Azul Marino",
            "categoria": "Jersey",
            "kg": 205.5,
            "kgn": 198.7,
            "importe": 32.5,
        },
        {  # cod/color/categoria vacíos -> None; kg/kgn None -> None
            "numero": None,
            "fecha": None,
            "fecha_terminado": None,
            "cod": "",
            "color": None,
            "categoria": "",
            "kg": None,
            "kgn": None,
            "importe": None,
        },
    ]
    with patch(
        "modules.tintura.service.formulas_db.fetch_all", return_value=rows
    ) as m:
        result = service.tinto_equiv_formulas(
            creacion_desde=date(2026, 7, 7), creacion_hasta=date(2026, 7, 31)
        )
    assert len(result) == 2
    o = result[0]
    assert o.numero == "24089"
    assert o.fecha == date(2026, 7, 7)
    assert o.fecha_terminado == date(2026, 7, 9)
    assert o.cod == "AZ-117"
    assert o.color == "Azul Marino"
    assert o.kg == 205.5
    assert o.kgn == 198.7
    assert o.importe == 32.5
    o2 = result[1]
    assert o2.numero == ""
    assert o2.cod is None and o2.color is None and o2.categoria is None
    assert o2.kg is None and o2.kgn is None
    assert o2.importe == 0.0
    # con excluir_lavados por defecto (True) el SQL filtra lavados
    sql, _params = m.call_args[0]
    assert "lavado" in sql.lower()


def test_tinto_equiv_formulas_sin_excluir_lavados_sin_fechas():
    with patch(
        "modules.tintura.service.formulas_db.fetch_all", return_value=[]
    ) as m:
        service.tinto_equiv_formulas(excluir_lavados=False)
    sql, _params = m.call_args[0]
    assert "lavado" not in sql.lower()


def test_tinto_equiv_orden_to_dict_serializa_fechas():
    o = service.TintoEquivOrden(
        numero="1",
        fecha=date(2026, 7, 7),
        fecha_terminado=date(2026, 7, 9),
        cod="X",
        color="Rojo",
        categoria="Jersey",
        kg=100.0,
        kgn=98.0,
        importe=10.0,
    )
    d = o.to_dict()
    assert d["fecha"] == "2026-07-07"
    assert d["fecha_terminado"] == "2026-07-09"
    # rama None de to_dict
    o2 = service.TintoEquivOrden(
        numero="2",
        fecha=None,
        fecha_terminado=None,
        cod=None,
        color=None,
        categoria=None,
        kg=None,
        kgn=None,
        importe=0.0,
    )
    d2 = o2.to_dict()
    assert d2["fecha"] is None
    assert d2["fecha_terminado"] is None


# ─── stock_colorante_fisico ──────────────────────────────────────────────
# TMT 2026-07-14 — cubre el branch de COLORANTE_FAMILIAS (POLI+ALG) que dejaba
# el gate de cobertura en 99.54% (líneas 589-590 sin ejercitar). La función
# valúa SÓLO colorante (POLI+ALG), ignora auxiliares, y es la única fuente del
# número que usan el balance y el flujo (banda STOCK DE QUÍMICOS / COLOR $).
def test_stock_colorante_fisico_suma_solo_poli_y_alg():
    """POLI+ALG se valúan (kg × $/kg); otras familias se ignoran; robusto a nulls."""
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(familia="POLI", stock_al_dia_kg=100.0, precio_us=2.0),  # 200
        SimpleNamespace(familia="alg", stock_al_dia_kg=50.0, precio_us=3.0),    # 150 (case-insensitive)
        SimpleNamespace(familia="AUX", stock_al_dia_kg=999.0, precio_us=9.0),   # ignorado
        SimpleNamespace(familia=None, stock_al_dia_kg="", precio_us=None),      # robusto a nulls
    ]
    with patch("modules.tintura.service.stock_quimicos_al_dia", return_value=rows):
        total = service.stock_colorante_fisico()
    assert total == pytest.approx(350.0)


def test_stock_colorante_fisico_bridge_vacio_es_cero():
    """Sin filas (bridge caído) devuelve 0.0 sin romper."""
    with patch("modules.tintura.service.stock_quimicos_al_dia", return_value=[]):
        assert service.stock_colorante_fisico() == 0.0
