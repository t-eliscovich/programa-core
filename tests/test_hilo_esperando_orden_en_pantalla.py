"""El hilo que espera orden se ve en las pantallas del hilo (TMT 2026-08-11).

Dueña, después de pedir que la barra amarilla saliera de las pantallas: *"si
necesito algo que lo muestre en tejeduría, pero distinto — quizás en hilo hay
9k que ya salieron pero no se descontaron hasta que esté la OFT"*.

No una alarma (esa es la campanita): un renglón más, al lado de "En máquinas",
para que el total cierre por la tabla y no por un número que no está escrito.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import hilo_sin_of as hs  # noqa: E402
from modules.asinfo import service  # noqa: E402


def _login_stock(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


_TOTALES = [{"id_bodega": 51, "total_kg": 1_984_291.0, "lotes": 10},
            {"id_bodega": 52, "total_kg": 250_000.0, "lotes": 5},
            {"id_bodega": 53, "total_kg": 348_000.0, "lotes": 5}]
_PROC = {"resumen": {"saldo": 30_473.0, "n_ofts": 3}, "por_tejido": [], "ofts": []}


def _pantalla(app, fake_db, esperando):
    c = _login_stock(app, fake_db)
    with patch.object(hs, "esperando_orden_kg", return_value=esperando), \
         patch.object(service, "stock_asinfo_lote_totales", return_value=_TOTALES), \
         patch.object(service, "fabricacion_proceso", return_value=_PROC), \
         patch.object(service, "disponible", return_value=True):
        return c.get("/stock/fabricacion-tc").get_data(as_text=True)


def test_el_renglon_aparece_con_sus_kilos(app, fake_db):
    html = _pantalla(app, fake_db, {51: 4192.0})
    assert "Despachado, esperando orden de fabricación" in html
    assert "4.192" in html


def test_el_hilo_total_CIERRA_por_la_tabla(app, fake_db):
    """Ésta es la razón de ser del renglón: sin él, "Hilo total" sale
    2.018.956 y los tres números de arriba suman 2.014.764. Un total que no
    cierra por lo que está escrito manda a buscar la diferencia a otro lado —
    que es exactamente lo que pasó toda la mañana del 11/08."""
    html = _pantalla(app, fake_db, {51: 4192.0})
    # 1.984.291 + 30.473 + 4.192
    assert "2.018.956" in html


def test_sin_nada_esperando_el_renglon_no_esta(app, fake_db):
    """Un renglón que dice cero es ruido: si no hay nada esperando, no está."""
    html = _pantalla(app, fake_db, {})
    assert "esperando orden de fabricación" not in html.lower()


def _tejeduria(app, fake_db, esperando):
    rid = fake_db.add_role("Tej", ["tejeduria.ver"])
    uid = fake_db.add_user("tej", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    with patch.object(hs, "esperando_orden_kg", return_value=esperando):
        return c.get("/produccion-tejeduria-asinfo").get_data(as_text=True)


def test_tejeduria_muestra_la_linea_arriba_de_todo(app, fake_db):
    """Ahí es donde alguien va a crear la orden: el número tiene que estar a
    la vista, en KILOS y hablando de la bodega."""
    html = _tejeduria(app, fake_db, {51: 4192.0})
    assert "4.192 kg de hilo" in html
    assert "orden de fabricación" in html


def test_tejeduria_sin_nada_esperando_no_dice_nada(app, fake_db):
    html = _tejeduria(app, fake_db, {})
    assert "kg de hilo" not in html


def test_las_tres_pantallas_tienen_su_lugar_para_el_dato():
    """Inventario y Flujo lo muestran como renglón; Tejeduría, como línea
    arriba de todo — ahí es donde alguien va a crear la orden."""
    # Flujo producción no se renderiza acá (arma media pantalla con Asinfo);
    # se fija que el renglón exista Y que cuelgue de la variable — cambiar la
    # condición por `False` es justamente el mutante que sobrevivía cuando esto
    # sólo buscaba el texto suelto.
    flujo = Path("modules/informes/templates/informes/flujo_produccion.html").read_text()
    assert "{% if espera_hilado or espera_crudo %}" in flujo
    assert "Esperando orden de fabricación" in flujo
    assert "{{ espera_hilado | num_es(0) }}" in flujo
    assert "{{ espera_crudo | num_es(0) }}" in flujo

    tej = Path("modules/tejeduria_asinfo/templates/tejeduria_asinfo/tab.html").read_text()
    assert "esperando_kg" in tej
    assert "orden de fabricación" in tej
    assert "utilidad" not in tej.lower()   # kilos y bodega, nunca la utilidad

    inv = Path("modules/stock_asinfo/templates/stock_asinfo/fabricacion.html").read_text()
    assert "f.espera" in inv
