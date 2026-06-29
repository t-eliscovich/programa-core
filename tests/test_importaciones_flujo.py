"""Flujo nuevo de importaciones: recibir (costo estimado → deuda) → pagar
(monto real sobrescribe deuda). Migraciones 0104+0107. Sin DB real."""
from __future__ import annotations

from unittest.mock import patch

from modules.importaciones import pago, service


# ── deuda neta = costo - anticipo (nunca negativa) ──────────────────────────
def test_deuda_neta_resta_anticipo():
    assert pago._deuda(30000, 0) == 30000.0
    assert pago._deuda(30000, 12000) == 18000.0
    assert pago._deuda(5000, 9000) == 0.0  # anticipo cubre de más → 0, no negativo


# ── set_recepcion calcula la deuda y marca recibido ─────────────────────────
def test_set_recepcion_genera_deuda():
    capt = {}
    def fake_upsert(prov, num, *, campos, valores, usuario):
        capt["campos"] = campos; capt["valores"] = valores
    with patch.object(pago, "_upsert", fake_upsert), \
         patch.object(pago, "today_ec", return_value="2026-06-29"):
        pago.set_recepcion("AC", 36, kg=1000, costo_estimado=30000, anticipo=12000)
    d = dict(zip([c.split("=")[0].strip() for c in capt["campos"]], capt["valores"], strict=False))
    assert d["recibido_pc"] is True
    assert d["kg_recibidos"] == 1000.0
    assert d["costo_estimado"] == 30000.0
    assert d["deuda"] == 18000.0  # 30000 - 12000


def test_set_recepcion_rechaza_kg_cero():
    import pytest
    with patch.object(pago, "_upsert"), pytest.raises(ValueError):
        pago.set_recepcion("AC", 36, kg=0, costo_estimado=30000)


# ── set_pago sobrescribe la deuda con el monto real ─────────────────────────
def test_set_pago_sobrescribe_deuda_30_a_32():
    capt = {}
    def fake_upsert(prov, num, *, campos, valores, usuario):
        capt["campos"] = campos; capt["valores"] = valores
    with patch.object(pago, "_upsert", fake_upsert), \
         patch.object(pago, "today_ec", return_value="2026-06-29"):
        # estimado era 30k, pago real 32k, anticipo 12k → deuda 20k
        pago.set_pago("AC", 36, monto_real=32000, anticipo=12000)
    d = dict(zip([c.split("=")[0].strip() for c in capt["campos"]], capt["valores"], strict=False))
    assert d["pagada"] is True
    assert d["monto_real"] == 32000.0
    assert d["deuda"] == 20000.0  # 32000 - 12000


# ── el cruce surface el estado de recepción/deuda en cada fila ──────────────
def _imp(nota="ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-1"):
    return {"im_numero": im, "fecha": "2026-06-01", "fecha_recepcion": None,
            "recibida": False, "bod": "", "total_asinfo": 1000.0,
            "proveedor": "ARIESCOPE", "prov_cod_asinfo": "AC", "nota": nota}


def test_cruce_surface_estado_recibido_y_deuda():
    estado = {("AC", 36): {
        "recibido_pc": True, "fecha_recepcion_pc": "2026-06-29", "kg_recibidos": 1000.0,
        "costo_estimado": 30000.0, "anticipo_aplicado": 0.0, "deuda": 30000.0,
        "pagada": False, "fecha_pago": None, "monto_real": None,
        "contabilizada": False, "monto_pagado": 0.0,
    }}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=[_imp()]), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(service, "promedios_usd_por_kg", return_value={"AC": 25.0}), \
         patch.object(pago, "estados_por_refs", return_value=estado):
        rows = service.importaciones_con_cruce()
    r = rows[0]
    assert r["recibido_pc"] is True
    assert r["deuda"] == 30000.0
    assert r["pagada"] is False
    assert r["estado_flujo"] == "recibida"
    # sugerencia de costo = promedio 25/kg × kg (kg viene de Asinfo; sin kg → None ok)


def test_kilos_pendientes_usa_recibido_no_pagado():
    estado = {("AC", 36): {
        "recibido_pc": True, "fecha_recepcion_pc": "2026-06-29", "kg_recibidos": 800.0,
        "costo_estimado": 30000.0, "anticipo_aplicado": 0.0, "deuda": 30000.0,
        "pagada": False, "fecha_pago": None, "monto_real": None,
        "contabilizada": False, "monto_pagado": 0.0,
    }}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=[_imp()]), \
         patch.object(service.asinfo_service, "importaciones_kg", return_value={"IM-1": 800.0}), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(service, "promedios_usd_por_kg", return_value={}), \
         patch.object(pago, "estados_por_refs", return_value=estado):
        res = service.kilos_pendientes_importaciones()
    assert res["n"] == 1
    assert res["kg"] == 800.0


def test_costo_estimado_por_hilado_y_preguntar():
    """El prefill usa el estimado por tipo de hilado (Asinfo); si faltan kg sin
    histórico, marca necesita_costo_manual (que pregunte)."""
    imp = [_imp()]
    ch = {"IM-1": {"costo": 71234.50, "kg": 24494.0, "usd_kg": 2.9082,
                   "kg_sin_precio": 0.0, "ventana": "3m"}}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=imp), \
         patch.object(service.asinfo_service, "importaciones_kg", return_value={"IM-1": 24494.0}), \
         patch.object(service.asinfo_service, "importaciones_costo_estimado", return_value=ch), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(service, "promedios_usd_por_kg", return_value={}), \
         patch.object(pago, "estados_por_refs", return_value={}):
        rows = service.importaciones_con_cruce()
    r = rows[0]
    assert r["costo_estimado_sugerido"] == 71234.50
    assert r["costo_ventana"] == "3m"
    assert r["necesita_costo_manual"] is False

    # Caso sin histórico 3/6m → kg_sin_precio>0 → preguntar
    ch2 = {"IM-1": {"costo": 0.0, "kg": 24494.0, "usd_kg": None,
                    "kg_sin_precio": 24494.0, "ventana": "parcial"}}
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=imp), \
         patch.object(service.asinfo_service, "importaciones_kg", return_value={"IM-1": 24494.0}), \
         patch.object(service.asinfo_service, "importaciones_costo_estimado", return_value=ch2), \
         patch.object(service.db, "fetch_all", return_value=[]), \
         patch.object(service, "promedios_usd_por_kg", return_value={}), \
         patch.object(pago, "estados_por_refs", return_value={}):
        rows = service.importaciones_con_cruce()
    assert rows[0]["necesita_costo_manual"] is True
