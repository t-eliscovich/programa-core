"""Leer la consulta de una card de Asinfo sin entrar a Metabase.

TMT 2026-08-07. La dueña preguntó si la retención dice en algún lado si está
pagada. La respuesta vive en el SQL de la card 202, y hasta ahora el único
lector de cards estaba clavado en la de facturas (199). Ahora se puede pedir
cualquiera — read-only, sólo el SQL y los nombres de las columnas.

⭐ Vale para cualquier pregunta del tipo "¿ese dato existe del otro lado?": es
mirar la fuente en vez de deducirla. Fue así como se descubrió que la card
filtra por fecha de FACTURA y no de cobro.
"""
from __future__ import annotations

import pytest


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


@pytest.fixture
def metabase(monkeypatch):
    """Metabase de mentira: devuelve la card que se le pida."""
    import requests

    from modules._lib import metabase_client as mc
    monkeypatch.setattr(mc, "_session_token", "tok", raising=False)
    monkeypatch.setenv("METABASE_URL", "http://mb.local")
    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    pedidos = []

    cards = {
        "199": {"id": 199, "name": "Facturas",
                "dataset_query": {"native": {"query": "SELECT numero FROM factura"}},
                "result_metadata": [{"name": "numero"}, {"name": "usd"}]},
        "202": {"id": 202, "name": "Retenciones",
                "dataset_query": {"native": {"query":
                    "SELECT f.numero, SUM(...) ret_fuente FROM detalle_cobro dc "
                    "JOIN forma_cobro fc ... WHERE f.fecha BETWEEN {{fecha_inicio}} "
                    "AND {{fecha_fin}}"}},
                "result_metadata": [{"name": "numero"}, {"name": "ret_fuente"},
                                    {"name": "ret_iva"}, {"name": "ret_total"}]},
    }

    def _get(url, headers=None, timeout=None):
        cid = url.rstrip("/").split("/")[-1]
        pedidos.append(cid)
        return _Resp(cards.get(cid, {}), 200 if cid in cards else 404)
    monkeypatch.setattr(requests, "get", _get)
    return pedidos


def test_sin_card_trae_la_de_facturas(metabase):
    from modules.admin_dbase.debug_asinfo_facturas_view import _card_facturas_get
    card, sql, path, err = _card_facturas_get()
    assert err is None and card["id"] == 199
    assert metabase == ["199"]


def test_con_card_trae_la_que_se_pide(metabase):
    """El punto del cambio: poder mirar la 202 (retenciones)."""
    from modules.admin_dbase.debug_asinfo_facturas_view import _card_facturas_get
    card, sql, path, err = _card_facturas_get("202")
    assert err is None and card["id"] == 202
    assert "detalle_cobro" in sql
    assert metabase == ["202"]


def test_la_card_inexistente_no_rompe(metabase):
    from modules.admin_dbase.debug_asinfo_facturas_view import _card_facturas_get
    card, sql, path, err = _card_facturas_get("9999")
    assert err and "9999" in err


def test_sin_metabase_avisa_en_vez_de_explotar(monkeypatch):
    from modules._lib import metabase_client as mc
    from modules.admin_dbase.debug_asinfo_facturas_view import _card_facturas_get
    monkeypatch.setenv("METABASE_URL", "")
    monkeypatch.setattr(mc, "_session_token", None, raising=False)
    monkeypatch.setattr(mc, "_login", lambda r: None)
    card, sql, path, err = _card_facturas_get("202")
    assert card is None and "Metabase" in err
