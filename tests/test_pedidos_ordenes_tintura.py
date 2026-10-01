"""Órdenes de tintura (/pedidos/ordenes) — estados para la bodega (TMT 2026-10-01)."""
from datetime import date
from unittest.mock import patch

from modules.pedidos import ordenes as o

HOY = date(2026, 10, 1)


def _fila(n, fecha, oft, ped=""):
    return {"numero": n, "fecha": fecha, "oft_numero": oft, "pedido_numero": ped,
            "plan_kg": "500", "producto": "Fleece", "producto_codigo": "FS96BLA"}


def test_estados_de_bodega():
    filas = [_fila("1", "01/10/2026", "OFT-1", "PDCL-1"), _fila("2", "18/09/2026", "OFT-2"),
             _fila("3", "25/09/2026", "OFT-3"), _fila("4", "25/09/2026", "OFT-4"),
             _fila("5", "25/09/2026", "OFT-5")]
    ofts = {"OFT-1": {"estado": 2, "plan_kg": 490, "fab_padre": 0, "fab_hijas": 0},
            "OFT-2": {"estado": 2, "plan_kg": 294, "fab_padre": 0, "fab_hijas": 0},
            "OFT-3": {"estado": 2, "plan_kg": 392, "fab_padre": 39, "fab_hijas": 0},
            "OFT-4": {"estado": 2, "plan_kg": 531, "fab_padre": 478, "fab_hijas": 0},
            "OFT-5": {"estado": 5, "plan_kg": 417, "fab_padre": 0, "fab_hijas": 371}}
    r = {f["orden"]: f for f in o.armar(filas, ofts, {"PDCL-1": {"codigo": "SVC", "nombre": "X"}}, HOY)}
    assert r["1"]["estado"] == "en_tintura" and r["1"]["dias"] == 0 and not r["1"]["demora"]
    assert r["1"]["cliente_cod"] == "SVC"
    assert r["2"]["estado"] == "en_tintura" and r["2"]["demora"]   # 13 días sin entrar nada
    assert r["3"]["estado"] == "parcial"
    # abierta con el 90%: para la bodega ya entró (el plan lleva la merma)
    assert r["4"]["estado"] == "en_bodega"
    # cerrada al 89% del plan: es lo normal, no se marca nada
    assert r["5"]["estado"] == "en_bodega" and r["5"]["fab"] == 371


def test_la_pantalla_muestra_codigo_cliente_y_kilos(app, fake_db):
    rid = fake_db.add_role("I", ["facturas.ver"])
    uid = fake_db.add_user("i", b"x", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    o.reset_cache()
    filas = [_fila("26-09-833", "26/09/2026", "OFT-000042209", "PDCL-31127")]
    ofts = {"OFT-000042209": {"estado": 5, "plan_kg": 417, "fab_padre": 371, "fab_hijas": 0}}
    with patch.object(o, "_ordenes_formulas", return_value=filas), \
         patch.object(o, "_ofts_asinfo", return_value=(ofts, True)), \
         patch.object(o, "_clientes_asinfo", return_value=({"PDCL-31127": {"codigo": "DPS", "nombre": "SANCHEZ"}}, True)):
        h = c.get("/pedidos/ordenes").get_data(as_text=True)
    assert "FS96BLA" in h and "DPS" in h and "PDCL-31127" in h
    assert "371" in h and "/ 417 kg" in h and "En bodega" in h


def test_formulas_caido_no_rompe(app, fake_db):
    rid = fake_db.add_role("I", ["facturas.ver"])
    uid = fake_db.add_user("i", b"x", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    o.reset_cache()
    with patch.object(o, "_ordenes_formulas", side_effect=RuntimeError("caído")):
        h = c.get("/pedidos/ordenes").get_data(as_text=True)
    assert "formulas no contestó" in h
