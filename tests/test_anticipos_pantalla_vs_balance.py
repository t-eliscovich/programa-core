"""La pantalla de anticipos y el balance tienen que decir lo mismo (TMT 2026-07-31).

Dueña: *"esencial, y que nunca más vuelva a pasar que esto nos va a mover la
utilidad por 20k"*.

Qué pasó: el 31/07 el balance empezó a descontar de ANTICIPOS los que ya tienen
la mercadería en la bodega —esos kilos ya están contados en el stock, contarlos
también como anticipo es contarlos dos veces—. La pantalla `/dolares` siguió
diciendo *"Total vivo · = ANTICIPOS del balance"*. Los dos números eran
correctos y medían cosas distintas, pero la pantalla afirmaba que eran el mismo:

    /dolares   2.156.425      "= ANTICIPOS del balance"
    /balance   2.138.525
    diferencia    17.900      ← media hora buscando un descalce inexistente

La regla que fijan estos tests: **la pantalla muestra la resta entera**, y el
resultado de esa resta ES el número del balance. Nada que deducir a mano.
"""
from unittest.mock import patch

import pytest

from modules.informes import queries as iq


@pytest.fixture(autouse=True)
def _limpio():
    iq._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    iq._ANTIC_RECIBIDOS_VISTOS.clear()
    yield
    iq._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    iq._ANTIC_RECIBIDOS_VISTOS.clear()


TOTAL_VIVO = 2_156_425.0
YA_EN_BODEGA = 17_900.0


def _cliente(app, fake_db):
    rid = fake_db.add_role("Tester", ["facturas.ver", "informes.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _pantalla(client):
    """GET /dolares con el resumen y el descuento mockeados."""
    with patch("modules.dolares.queries.resumen",
               return_value={"total_vivos": TOTAL_VIVO, "n_vivos": 122,
                             "n_ctas_vivos": 8, "total_aplicados": 34_274_100.0,
                             "n_aplicados": 3070}), \
         patch("modules.dolares.queries.lista", return_value=[]), \
         patch("modules.dolares.queries.por_cuenta", return_value=[]), \
         patch("modules.dolares.queries.tipos_por_cuenta", return_value={}), \
         patch.object(iq, "anticipos_con_mercaderia_recibida",
                      return_value=YA_EN_BODEGA):
        return client.get("/dolares")


def test_la_pantalla_ya_no_miente_sobre_el_balance(app, fake_db):
    rv = _pantalla(_cliente(app, fake_db))
    html = rv.get_data(as_text=True)
    # La frase vieja NO puede volver: era la que afirmaba la igualdad falsa.
    assert "Total vivo</div>" not in html or "= ANTICIPOS del balance" not in html


def test_las_tres_partes_de_la_resta_estan_a_la_vista(app, fake_db):
    rv = _pantalla(_cliente(app, fake_db))
    html = rv.get_data(as_text=True)
    assert "Ya en bodega" in html
    assert "Va al balance" in html
    assert "2.138.525" in html      # 2.156.425 − 17.900, escrito


def test_la_resta_da_exactamente_el_anticipos_del_balance():
    """El invariante de fondo, sin pantalla de por medio."""
    with patch.object(iq, "anticipos_con_mercaderia_recibida",
                      return_value=YA_EN_BODEGA), \
         patch.object(iq.db, "fetch_one", return_value={"total": TOTAL_VIVO}):
        del_balance = iq.anticipos()
    assert del_balance == round(TOTAL_VIVO - YA_EN_BODEGA, 2) == 2_138_525.0


def test_sin_nada_en_bodega_los_dos_numeros_coinciden():
    """El caso de siempre: si no llegó nada, la resta no cambia nada."""
    with patch.object(iq, "anticipos_con_mercaderia_recibida", return_value=0.0), \
         patch.object(iq.db, "fetch_one", return_value={"total": TOTAL_VIVO}):
        assert iq.anticipos() == TOTAL_VIVO


def test_si_falla_el_descuento_la_pantalla_no_se_cae(app, fake_db):
    """Un KPI no puede tumbar la pantalla de anticipos."""
    client = _cliente(app, fake_db)
    with patch("modules.dolares.queries.resumen",
               return_value={"total_vivos": TOTAL_VIVO, "n_vivos": 1,
                             "n_ctas_vivos": 1, "total_aplicados": 0.0,
                             "n_aplicados": 0}), \
         patch("modules.dolares.queries.lista", return_value=[]), \
         patch("modules.dolares.queries.por_cuenta", return_value=[]), \
         patch("modules.dolares.queries.tipos_por_cuenta", return_value={}), \
         patch.object(iq, "anticipos_con_mercaderia_recibida",
                      side_effect=RuntimeError("Asinfo caído")):
        rv = client.get("/dolares")
    assert rv.status_code in (200, 302)
