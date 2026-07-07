"""Flujo de importaciones — modelo v2 (TMT 2026-07-06 dueña, SIMPLIFICADO):
RECIBIR mete los kg al stock; los ANTICIPOS son movimientos (Σ = valor del
stock); el restante se carga por /compras. Sin flujo "Pagar" ni predicción
de costo. Sin DB real."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.importaciones import pago, service


# ── set_recepcion marca recibido + kg (sin deuda ni anticipo) ────────────────
def test_set_recepcion_marca_recibido_y_kg():
    capt = {}
    def fake_upsert(im_numero, prov, num, *, campos, valores, usuario, conn=None):
        capt["campos"] = campos; capt["valores"] = valores
    with patch.object(pago, "_upsert", fake_upsert), \
         patch.object(pago, "today_ec", return_value="2026-07-06"):
        pago.set_recepcion("IM-1", "AC", 36, kg=1000)
    d = dict(zip([c.split("=")[0].strip() for c in capt["campos"]], capt["valores"], strict=False))
    assert d["recibido_pc"] is True
    assert d["kg_recibidos"] == 1000.0
    # v2: recibir NO genera deuda ni toca anticipos (dejamos de predecir)
    assert "deuda" not in d
    assert "anticipo_aplicado" not in d
    assert "costo_estimado" not in d  # sin costo → ni se escribe


def test_set_recepcion_costo_solo_referencia_opcional():
    capt = {}
    def fake_upsert(im_numero, prov, num, *, campos, valores, usuario, conn=None):
        capt["campos"] = campos; capt["valores"] = valores
    with patch.object(pago, "_upsert", fake_upsert), \
         patch.object(pago, "today_ec", return_value="2026-07-06"):
        pago.set_recepcion("IM-1", "AC", 36, kg=1000, costo_estimado=30000)
    d = dict(zip([c.split("=")[0].strip() for c in capt["campos"]], capt["valores"], strict=False))
    assert d["costo_estimado"] == 30000.0  # referencia histórica, nada lo valúa
    assert "deuda" not in d


def test_set_recepcion_rechaza_kg_cero():
    with patch.object(pago, "_upsert"), pytest.raises(ValueError):
        pago.set_recepcion("IM-1", "AC", 36, kg=0)


# ── deshacer_recepcion no toca los anticipos ─────────────────────────────────
def test_deshacer_recepcion_solo_limpia_recepcion():
    capt = {}
    def fake_upsert(im_numero, prov, num, *, campos, valores, usuario, conn=None):
        capt["campos"] = campos; capt["valores"] = valores
    with patch.object(pago, "_upsert", fake_upsert):
        pago.deshacer_recepcion("IM-1", "AC", 36)
    keys = [c.split("=")[0].strip() for c in capt["campos"]]
    assert "recibido_pc" in keys and "kg_recibidos" in keys
    # los anticipos (movimientos + ND) son plata real: NO se tocan acá
    assert "anticipo_aplicado" not in keys
    assert "deuda" not in keys


# ── el cruce surface recepción + Σ anticipos (= valor stock) por fila ────────
def _imp(nota="ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-1"):
    return {"im_numero": im, "fecha": "2026-06-01", "fecha_recepcion": None,
            "recibida": False, "bod": "", "total_asinfo": 1000.0,
            "proveedor": "ARIESCOPE", "prov_cod_asinfo": "AC", "nota": nota}


def test_cruce_surface_recibido_y_suma_anticipos():
    estado = {"IM-1": {
        "recibido_pc": True, "fecha_recepcion_pc": "2026-06-29",
        "kg_recibidos": 1000.0, "costo_estimado": 30000.0,
        "anticipo_aplicado": 999.0,  # cache viejo — mandan los movimientos
    }}
    movs = {"IM-1": [
        {"id_mov": 1, "tipo": "anticipo", "fecha": "2026-07-01", "monto": 12000.0,
         "nota": "", "id_transaccion": 9001},
        {"id_mov": 2, "tipo": "anticipo", "fecha": "2026-07-03", "monto": 5000.0,
         "nota": "", "id_transaccion": 9002},
    ]}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=[_imp()]), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(pago, "estados_por_im", return_value=estado), \
         patch.object(pago, "movimientos_por_im", return_value=movs):
        rows = service.importaciones_con_cruce()
    r = rows[0]
    assert r["recibido_pc"] is True
    assert r["kg_recibidos"] == 1000.0
    # VALOR DEL STOCK = Σ anticipos (fuente de verdad: los movimientos)
    assert r["anticipo_aplicado"] == 17000.0
    assert len(r["movimientos"]) == 2
    # v2: nada de deuda/pagada/estado_flujo como concepto de la fila
    assert "deuda" not in r
    assert "pagada" not in r
    assert "estado_flujo" not in r


def test_cruce_sin_movimientos_usa_cache():
    """Fail-soft: si la mig 0113 no corrió (movimientos {}), vale el cache."""
    estado = {"IM-1": {
        "recibido_pc": False, "fecha_recepcion_pc": None, "kg_recibidos": None,
        "costo_estimado": None, "anticipo_aplicado": 8000.0,
    }}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=[_imp()]), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(pago, "estados_por_im", return_value=estado), \
         patch.object(pago, "movimientos_por_im", return_value={}):
        rows = service.importaciones_con_cruce()
    assert rows[0]["anticipo_aplicado"] == 8000.0
    assert rows[0]["movimientos"] == []
