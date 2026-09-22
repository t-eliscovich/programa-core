"""Excel del flujo — fórmula de Federico (2026-09-22):
saldo del día = saldo anterior − gastos forzados − posdatados + cheques."""
from datetime import date

from modules.informes.queries import construir_flujo_excel

HOY = date(2026, 9, 22)


def test_formula_y_encadenado():
    items = [
        {"fecha": date(2026, 9, 25), "tipo": "forzado", "importe": 100},
        {"fecha": date(2026, 9, 25), "tipo": "posdat", "importe": 50},
        {"fecha": date(2026, 9, 25), "tipo": "cheque", "importe": 30},
        {"fecha": date(2026, 10, 1), "tipo": "posdat", "importe": 10},
    ]
    f = construir_flujo_excel(HOY, 1000, items)
    assert [r["fecha"] for r in f] == [date(2026, 9, 25), date(2026, 10, 1)]
    assert f[0]["saldo_anterior"] == 1000
    assert f[0]["saldo"] == 1000 - 100 - 50 + 30
    assert f[1]["saldo_anterior"] == f[0]["saldo"]
    assert f[1]["saldo"] == 870


def test_vencidos_van_a_manana_y_no_se_pierden():
    # Forzado manual con fecha de ene-2026 por error: tiene que restar mañana.
    items = [{"fecha": date(2026, 1, 26), "tipo": "forzado", "importe": 150000}]
    f = construir_flujo_excel(HOY, 0, items)
    assert f == [{"fecha": date(2026, 9, 23), "saldo_anterior": 0,
                  "forzados": 150000, "posdatados": 0, "emitidos": 0, "cheques": 0,
                  "saldo": -150000}]


def test_fuera_de_horizonte_no_entra():
    items = [{"fecha": date(2028, 1, 1), "tipo": "posdat", "importe": 5}]
    assert construir_flujo_excel(HOY, 0, items, dias_adelante=365) == []


def test_emitidos_banc_1_2_restan_aparte():
    items = [{"fecha": date(2026, 9, 30), "tipo": "emitido", "importe": 40},
             {"fecha": date(2026, 9, 30), "tipo": "posdat", "importe": 60}]
    f = construir_flujo_excel(HOY, 500, items)
    assert f[0]["emitidos"] == 40 and f[0]["posdatados"] == 60
    assert f[0]["saldo"] == 400
