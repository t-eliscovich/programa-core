"""La misma factura de proveedor no sale dos veces del banco sin que nadie lo vea.

Tamara 2026-09-23 (caso AQ 166). El 20/08 un "Registrar banco" de /posdat
grabó una ND de $ 6.603,30 por la factura 166 de AQ que el banco nunca
debitó; el 05/09 la misma 166 se pagó dentro de "AQ 165/166/167/168/174/175/
177/178" ($ 26.438,63, PAG-CASH del 04/09). Un mes de pendiente sin
contraparte en la conciliación.

Tres frenos, tres grupos de tests:
  1. /bancos/emitir-cheque pregunta ¿YA PAGADA? antes de grabar.
  2. /admin/health/debito-sin-banco avisa el débito que el banco no tiene y la
     factura pagada dos veces.
  3. El modal de "Registrar banco" en /posdat no se dispara con el teclado ni
     manda tres veces, y dice cuándo vence.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.bancos import pago_repetido as pr  # noqa: E402

# El caso real, tal cual está en producción.
DEBITO_166 = {
    "id_transaccion": 46115, "no_banco": 10, "fecha": date(2026, 8, 20),
    "importe": 6603.30, "prov": "AQ",
    "concepto": "Débito posdat AQ 166          30",
}
PAGO_0509 = "AQ 165/166/167/168/174/175/177/178"


# ---------------------------------------------------------------------------
# 1. Partes puras
# ---------------------------------------------------------------------------


def test_numeros_del_concepto_del_pago():
    assert pr.numeros_de(PAGO_0509) == {
        "165", "166", "167", "168", "174", "175", "177", "178"}
    assert pr.numeros_de("FACT 0166") == {"166"}
    assert pr.numeros_de("") == set()


def test_factura_del_debito():
    assert pr.factura_del_debito(DEBITO_166["concepto"], "AQ") == "166"
    assert pr.factura_del_debito("Débito posdat ES 6756 4", "ES") == "6756"
    # Sin número no hay con qué comparar — texto suelto da falsos positivos.
    assert pr.factura_del_debito("Débito posdat CC QUIMSERTEC 2184", "CC") is None
    # Cuotas ("4/48") no son una factura: se repiten todos los meses.
    assert pr.factura_del_debito("Débito posdat BP 4/48", "BP") is None


def test_el_pago_del_0509_coincide_con_la_166():
    """⭐ LA REGRESIÓN: el pago del 05/09 nombraba la 166 ya debitada."""
    hits = pr.coincidencias([DEBITO_166], PAGO_0509, {"AQ"})
    assert [h["factura"] for h in hits] == ["166"]


def test_otro_proveedor_con_el_mismo_numero_no_coincide():
    assert pr.coincidencias([DEBITO_166], "NQ 166", {"NQ"}) == []


def test_un_pago_que_no_nombra_la_factura_no_coincide():
    assert pr.coincidencias([DEBITO_166], "AQ 195/198/199/200", {"AQ"}) == []


def test_el_proveedor_sale_del_concepto_o_de_los_posdatados():
    assert pr.provs_del_pago(PAGO_0509, set(), {"AQ", "BP"}) == {"AQ"}
    assert pr.provs_del_pago("165/166", {"aq"}, set()) == {"AQ"}
    # "CMB VEPAMIL" no es un proveedor con débitos: no inventa uno.
    assert pr.provs_del_pago("CMB VEPAMIL", set(), {"AQ"}) == set()


def test_health_alerta_el_debito_que_el_extracto_ya_dejo_atras():
    alerts, stats = pr.evaluar_sin_banco(
        [DEBITO_166], {10: date(2026, 9, 18)}, [])
    assert stats["n_sin_banco"] == 1
    assert alerts and alerts[0]["category"] == "debito_posdat_sin_banco"
    assert "AQ 166" in alerts[0]["msg"]


def test_health_no_alerta_mientras_el_extracto_no_llega():
    # Extracto hasta el 22/08: el débito del 20/08 todavía puede aparecer.
    alerts, stats = pr.evaluar_sin_banco(
        [DEBITO_166], {10: date(2026, 8, 22)}, [])
    assert alerts == [] and stats["n_sin_banco"] == 0


def test_health_alerta_la_factura_pagada_dos_veces():
    rep = [{"prov": "AQ", "factura": "166", "fecha": date(2026, 8, 20),
            "id_transaccion": 46115, "otro_id": 46847,
            "otro_fecha": date(2026, 9, 5), "otro_concepto": PAGO_0509}]
    alerts, stats = pr.evaluar_sin_banco([], {}, rep)
    assert stats["n_repetidas"] == 1
    assert alerts[0]["category"] == "factura_pagada_dos_veces"
    assert "AQ 166" in alerts[0]["msg"]


# ---------------------------------------------------------------------------
# 2. /bancos/emitir-cheque pregunta antes de grabar
# ---------------------------------------------------------------------------


@pytest.fixture
def cliente_y_queries(monkeypatch):
    from modules.bancos import views as bancos_views
    from tests.test_banco_fecha_vieja_se_confirma import _FakeQueries
    from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    fake = _FakeQueries()
    previo = bancos_views.queries
    bancos_views.queries = fake
    llamadas = []

    def _ya(**kw):
        llamadas.append(kw)
        return pr.coincidencias([DEBITO_166], kw["concepto"], {"AQ"})

    monkeypatch.setattr(pr, "ya_debitadas", _ya)
    try:
        yield app.test_client(), fake, llamadas
    finally:
        bancos_views.queries = previo
        deshacer()


def _pago(concepto, *extra):
    from modules.bancos import views as bancos_views
    hoy = bancos_views.today_ec()
    return MultiDict([("documento", "ND"), ("tipo", "proveedor"),
                      ("no_banco", "10"), ("importe", "26438,63"),
                      ("fecha", hoy.isoformat()), ("concepto", concepto),
                      *extra])


def test_pagar_una_factura_ya_debitada_pregunta_y_no_graba(cliente_y_queries):
    cliente, fake, _ = cliente_y_queries
    r = cliente.post("/bancos/emitir-cheque", data=_pago(PAGO_0509))
    assert fake.creado is None, (
        "grabó el pago de la 166 sin avisar que ya había salido del banco — "
        "es exactamente cómo la 166 de AQ salió dos veces")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "YA PAGADA" in html and "AQ 166" in html
    assert 'name="permitir_ya_pagada"' in html


def test_confirmar_que_es_otra_factura_graba(cliente_y_queries):
    cliente, fake, _ = cliente_y_queries
    r = cliente.post("/bancos/emitir-cheque",
                     data=_pago(PAGO_0509, ("permitir_ya_pagada", "1")))
    assert r.status_code == 302
    assert fake.creado is not None


def test_un_pago_sin_facturas_debitadas_no_pregunta(cliente_y_queries):
    cliente, fake, _ = cliente_y_queries
    r = cliente.post("/bancos/emitir-cheque", data=_pago("AQ 195/198/199/200"))
    assert r.status_code == 302 and fake.creado is not None


def test_si_el_freno_falla_el_pago_igual_se_graba(cliente_y_queries, monkeypatch):
    cliente, fake, _ = cliente_y_queries

    def _rompe(**kw):
        raise RuntimeError("base caída")

    monkeypatch.setattr(pr, "ya_debitadas", _rompe)
    r = cliente.post("/bancos/emitir-cheque", data=_pago(PAGO_0509))
    assert r.status_code == 302 and fake.creado is not None


# ---------------------------------------------------------------------------
# 3. El modal de "Registrar banco"
# ---------------------------------------------------------------------------

_LISTA = os.path.join(_REPO_ROOT, "modules", "posdat", "templates", "posdat",
                      "lista.html")


def _lista():
    with open(_LISTA, encoding="utf-8") as fh:
        return fh.read()


def test_el_boton_no_se_alcanza_con_tab():
    html = _lista()
    i = html.index('class="posdat-registrar-banco')
    boton = html[html.rindex("<button", 0, i):i]
    assert 'tabindex="-1"' in boton


def test_el_modal_no_manda_con_enter_ni_dos_veces():
    html = _lista()
    assert "e.key === 'Enter' && e.target.tagName === 'INPUT'" in html
    assert "form.dataset.enviado === '1'" in html


def test_el_modal_dice_cuando_vence():
    html = _lista()
    assert 'data-fechad=' in html
    assert 'id="rb-vence"' in html
    assert "todavía faltan" in html
