"""Kg en importaciones (TMT 2026-06-10 dueña: "importaciones no dice kg")."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_importaciones_con_cruce_merge_kg(monkeypatch):
    from modules.asinfo import service as asinfo_service
    from modules.importaciones import service as imp_service

    rows = [{"im_numero": "IM-0001", "nota": "", "total_asinfo": 100.0},
            {"im_numero": "IM-0002", "nota": "", "total_asinfo": 200.0}]
    with patch.object(asinfo_service, "importaciones_asinfo", return_value=rows), \
         patch.object(asinfo_service, "importaciones_kg",
                      return_value={"IM-0001": 1234.5}), \
         patch.object(imp_service, "_buscar_compras", return_value={}), \
         patch.object(imp_service, "_buscar_anticipos", return_value={}):
        out = imp_service.importaciones_con_cruce()
    assert out[0]["kg"] == 1234.5
    assert out[1]["kg"] is None  # sin detalle → la vista muestra —


def test_importaciones_kg_fail_soft(monkeypatch):
    """Si el discovery no encuentra la tabla de detalle → {} sin romper."""
    from modules.asinfo import service as svc

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", lambda *a, **k: [])
    svc._IMPORT_KG_CACHE.clear()
    svc._IMPORT_KG_DETALLE.clear()
    assert svc.importaciones_kg() == {}


def test_importaciones_kg_descubre_y_suma(monkeypatch):
    from modules.asinfo import service as svc

    calls = []

    def fake_fetch(db, sql, max_results=100):
        calls.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            return [{"tabla": "detalle_factura_proveedor", "col": "cantidad"}]
        return [{"im_numero": "IM-0001", "kg": 4321.0}]

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", fake_fetch)
    svc._IMPORT_KG_CACHE.clear()
    svc._IMPORT_KG_DETALLE.clear()
    out = svc.importaciones_kg()
    assert out == {"IM-0001": 4321.0}
    assert any("detalle_factura_proveedor" in s for s in calls)
