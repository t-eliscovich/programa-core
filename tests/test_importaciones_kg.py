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


def test_descubrimiento_caido_usa_el_nombre_conocido(monkeypatch):
    """Regresión 2026-08-30: el descubrimiento fallaba UNA vez (Metabase caído
    al arrancar) y quedaba cacheado para siempre — importaciones_kg() devolvía
    {} hasta el próximo deploy, /importaciones mostraba KG en «—» y el balance
    mandaba a completar a mano 22 compras cuyos kg estaban sanos en Asinfo.
    Si el descubrimiento no contesta, los kg salen del nombre conocido."""
    from modules.asinfo import service as svc

    def fake_fetch(db, sql, max_results=100):
        if "INFORMATION_SCHEMA" in sql:
            return []  # Metabase no contestó el descubrimiento
        assert "detalle_factura_proveedor" in sql  # nombre conocido
        return [{"im_numero": "IM-0000572", "kg": 11289.39}]

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", fake_fetch)
    svc._IMPORT_KG_CACHE.clear()
    svc._IMPORT_KG_DETALLE.clear()
    assert svc.importaciones_kg() == {"IM-0000572": 11289.39}


def test_descubrimiento_caido_reintenta_en_la_proxima(monkeypatch):
    """El fracaso del descubrimiento NO queda cacheado: la llamada siguiente
    vuelve a preguntar por INFORMATION_SCHEMA (antes `done=True` con hit=None
    lo enterraba hasta el reinicio)."""
    from modules.asinfo import service as svc

    consultas = []

    def fake_fetch(db, sql, max_results=100):
        if "INFORMATION_SCHEMA" in sql:
            consultas.append(sql)
            return []
        return []

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", fake_fetch)
    svc._IMPORT_KG_CACHE.clear()
    svc._IMPORT_KG_DETALLE.clear()
    svc._descubrir_detalle_fp()
    svc._descubrir_detalle_fp()
    assert len(consultas) == 2


def test_sin_match_anota_la_importacion_cuando_matchea_sin_kg():
    """kg_hilado_mes: la compra que matchea una importación SIN kg en Asinfo
    sale en sin_match CON el nº de importación — el balance la separa de la
    que no matchea nada (a esa sí se le completa el N°/TIPO a mano)."""
    from unittest.mock import patch as _patch

    from modules.importaciones import service as isvc

    index = {("AI", 15): [{"im_numero": "IM-0000572", "grupo_id": "IM-0000572",
                           "grupo_kg": 0.0, "kg": None, "fecha": "2026-05-05",
                           "fecha_recepcion": "2026-08-01"}]}
    compras = [
        {"prov": "AI", "ref": 15, "fecha": "2026-08-01", "kg": 0, "importe": 58367.20},
        {"prov": "ZZ", "ref": 99, "fecha": "2026-08-02", "kg": 0, "importe": 9999.0},
    ]
    with _patch.object(isvc, "_index_importaciones_por_codigo", return_value=index):
        out = isvc.kg_hilado_mes(compras, mes="2026-08")
    con_im = [c for c in out["sin_match"] if c.get("im")]
    sin_im = [c for c in out["sin_match"] if not c.get("im")]
    assert [c["prov"] for c in con_im] == ["AI"]
    assert con_im[0]["im"] == "IM-0000572"
    # la compra sin importación en el índice no está en sin_match (kg propio manda)
    assert sin_im == []
