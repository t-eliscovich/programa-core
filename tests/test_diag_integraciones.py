"""Tests de /diag/integraciones — vista de diagnóstico del bridge.

Cubre:
    - Renderiza 200 cuando los bridges están vacíos (no configurados).
    - Renderiza 200 con data mockeada de tintura y asinfo.
    - Acepta filtros desde/hasta en query string.
    - Requiere permiso informes.ver.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.asinfo import service as asinfo_service
from modules.tintura import service as tintura_service
from modules.tintura.service import StockProducto, TinturadoOrden


def _login(app, fake_db, permisos=("informes.ver",)):
    rid = fake_db.add_role("Informes", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_diag_integraciones_responde_200_aunque_bridges_vacios(app, fake_db):
    """Sin data del bridge, igual debe renderizar 200 con placeholders."""
    c = _login(app, fake_db)
    with patch.object(tintura_service, "tinturado_resumen", return_value=[]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value={}), \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones")
    assert r.status_code == 200, r.data[:400]
    body = r.get_data(as_text=True)
    assert "Diagnóstico" in body
    assert "Tintorería" in body
    assert "Asinfo" in body


def test_diag_integraciones_muestra_orden_de_tintura(app, fake_db):
    o = TinturadoOrden(
        numero="24500",
        fecha=date(2026, 5, 20),
        fecha_terminado=date(2026, 5, 21),
        formula_cod="AZ-117",
        color="Azul Marino",
        categoria="Jersey",
        kilos_planeados=200.0,
        tela_cruda_kg=205.0,
        tela_terminada_kg=198.0,
        desperdicio_kg=7.0,
        jet=3,
        es_reproceso=False,
        observaciones=None,
    )
    c = _login(app, fake_db)
    with patch.object(tintura_service, "tinturado_resumen", return_value=[o]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value={}), \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones")
    body = r.get_data(as_text=True)
    assert "24500" in body
    assert "Azul Marino" in body


def test_diag_integraciones_muestra_totales_asinfo(app, fake_db):
    totales = {
        "FACTURA":       {"docs": 88, "kg": 11113.03, "usd": 96241.14},
        "DEVOLUCION":    {"docs": 10, "kg": -266.70,  "usd": -2122.81},
        "NC_FINANCIERA": {"docs": 7,  "kg": 0.0,      "usd": -156.10},
        "NTEN":          {"docs": 6,  "kg": 812.75,   "usd": 6769.72},
    }
    c = _login(app, fake_db)
    with patch.object(tintura_service, "tinturado_resumen", return_value=[]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value=totales), \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones")
    body = r.get_data(as_text=True)
    assert "FACTURA" in body
    assert "NC_FINANCIERA" in body
    assert "DEVOLUCION" in body


def test_diag_integraciones_acepta_filtros_de_fecha(app, fake_db):
    """desde/hasta de query string llegan al service de Asinfo."""
    c = _login(app, fake_db)
    with patch.object(tintura_service, "tinturado_resumen", return_value=[]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value={}) as m_tot, \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones?desde=2026-05-01&hasta=2026-05-31")
    assert r.status_code == 200
    args = m_tot.call_args[0]
    assert args[0] == date(2026, 5, 1)
    assert args[1] == date(2026, 5, 31)


def test_diag_integraciones_fecha_invalida_default_a_hoy(app, fake_db):
    """Si querystring trae fecha inválida, default a hoy en lugar de levantar."""
    c = _login(app, fake_db)
    with patch.object(tintura_service, "tinturado_resumen", return_value=[]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value={}), \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones?desde=tonteria&hasta=otra-tonteria")
    assert r.status_code == 200


def test_diag_integraciones_sin_permiso_redirige(app, fake_db):
    """Usuario sin informes.ver no entra."""
    c = _login(app, fake_db, permisos=())  # sin permisos
    with patch.object(tintura_service, "tinturado_resumen", return_value=[]), \
         patch.object(tintura_service, "stock_quimicos", return_value=[]), \
         patch.object(asinfo_service, "facturas_totales_por_tipo", return_value={}), \
         patch.object(asinfo_service, "facturas_periodo", return_value=[]):
        r = c.get("/diag/integraciones", follow_redirects=False)
    # 302/403 ambos son válidos según cómo esté armado @requiere_permiso
    assert r.status_code in (302, 403)
