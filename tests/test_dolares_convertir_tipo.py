"""La conversión desde la barra de selección tipa la compra según el PROVEEDOR
(server-side, no el frente):

  · tipo 'Q' (químicos)   → compra tipo Q.
  · tipo 'U' (maquinaria) → NO se convierte (va por "Activar máquina").
  · resto (hilado)        → compra tipo H.

TMT 2026-07-11.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules.dolares import queries as dq


def _login(app, fake_db, permisos=("compras.crear",)):
    rid = fake_db.add_role("Compras", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _post(c, cta):
    return c.post(
        "/dolares/convertir-seleccion",
        data={"codigo_prov": cta, "id_dolares": ["1"], "concepto": "20"},
    )


def _stub_result():
    return {
        "n_anticipos": 1, "numero_compra": 5,
        "comprobante": "BAP-5", "importe_total": 100.0,
    }


def test_quimico_convierte_tipo_Q(app, fake_db):
    c = _login(app, fake_db)
    conv = MagicMock(return_value=_stub_result())
    with patch.object(dq, "tipos_por_cuenta", return_value={"AQ": "Q"}), \
         patch.object(dq, "convertir_a_compra", conv):
        _post(c, "AQ")
    assert conv.called
    assert conv.call_args.kwargs["tipo_compra"] == "Q"


def test_hilado_convierte_tipo_H(app, fake_db):
    c = _login(app, fake_db)
    conv = MagicMock(return_value=_stub_result())
    with patch.object(dq, "tipos_por_cuenta", return_value={"AH": "H"}), \
         patch.object(dq, "convertir_a_compra", conv):
        _post(c, "AH")
    assert conv.called
    assert conv.call_args.kwargs["tipo_compra"] == "H"


def test_maquinaria_no_convierte(app, fake_db):
    c = _login(app, fake_db)
    conv = MagicMock(return_value=_stub_result())
    with patch.object(dq, "tipos_por_cuenta", return_value={"AU": "U"}), \
         patch.object(dq, "convertir_a_compra", conv):
        r = _post(c, "AU")
    assert not conv.called, "maquinaria no debe crear una compra"
    assert r.status_code in (302, 303)


def test_es_proveedor_quimico_helper():
    with patch.object(dq, "tipos_por_cuenta", return_value={"AQ": "Q"}):
        assert dq.es_proveedor_quimico("aq") is True
    with patch.object(dq, "tipos_por_cuenta", return_value={"AU": "U"}):
        assert dq.es_proveedor_quimico("AU") is False
    assert dq.es_proveedor_quimico("") is False
