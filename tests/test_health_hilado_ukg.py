"""El $/kg del hilado tiene que poder reconstruirse — health diario.

El 07/08/2026 el hilado estuvo ocho días valuado a 3,0717 US$/kg cuando
correspondía 3,0387, porque la apertura del mes salía de una tarifa de CIERRE y
el promedio ponderado le diluía las compras del mes dos veces. El síntoma
estuvo a la vista desde el 31/07 17:56 y no había nada mirándolo.

Este health mira el síntoma, no el bug: si el $/kg no se reconstruye con
`apertura + compras del mes`, algo lo está moviendo por fuera.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_dbase import health_audit_view as hv  # noqa: E402

# Producción, 07/08/2026.
APERTURA = 3.0200921
HILADO_KG = 1978794.0
COMPRAS_KG = 154400.0
COMPRAS_US = 504819.71
UKG_SANO = 3.0387    # después del fix
UKG_ROTO = 3.0717    # lo que se mostraba con la doble dilución


def _foto(ukg, hil_kg=HILADO_KG, com_kg=COMPRAS_KG, com_us=COMPRAS_US):
    return {"creado_en": "2026-08-07T11:35:00+00:00", "hilado_kg": hil_kg,
            "hilado_ukg": ukg, "compras_kg": com_kg, "compras_us": com_us,
            "kg_sin_costo": 0.0}


def _correr(fila, apertura=APERTURA):
    app = hv.__dict__.get("_test_app")
    from flask import Flask
    app = app or Flask(__name__)
    with app.test_request_context("/admin/health/hilado-ukg"), \
         patch.object(hv.db, "fetch_one", return_value=fila), \
         patch("modules.informes.queries.apertura_ukg_hilado", return_value=apertura):
        resp = hv.hilado_ukg_reconstruible.__wrapped__.__wrapped__()
    return resp.get_json()


def test_el_ukg_corregido_no_alerta():
    out = _correr(_foto(UKG_SANO))
    assert out["ok"] is True
    assert out["alerts"] == []
    assert round(out["stats"]["ukg_esperado"], 4) == 3.0396
    assert abs(out["stats"]["gap_us"]) < hv._HILADO_UKG_TOL_US


def test_la_doble_dilucion_habria_alertado():
    """Con el $/kg de antes del fix, el health se prende."""
    out = _correr(_foto(UKG_ROTO))
    assert out["ok"] is False
    assert [a["category"] for a in out["alerts"]] == ["hilado_ukg_no_reconstruible"]
    assert out["alerts"][0]["severity"] == "high"
    assert round(out["stats"]["gap_us"], 0) == 63604.0
    assert "APERTURA" in out["alerts"][0]["msg"]


def test_kilos_que_entran_sin_su_plata_tambien_se_ven():
    """El caso inverso: los kg ya están y la compra todavía no se cargó.

    El promedio se diluye de gratis y el $/kg cae por debajo de lo reconstruible.
    """
    out = _correr(_foto(2.98))
    assert out["ok"] is False
    assert out["stats"]["gap_us"] < 0


def test_sin_fotos_no_rompe():
    out = _correr(None)
    assert out["ok"] is True
    assert out["alerts"][0]["category"] == "sin_traza"


def test_sin_apertura_del_mes_anterior_no_rompe():
    out = _correr(_foto(UKG_SANO), apertura=0.0)
    assert out["ok"] is True
    assert out["alerts"][0]["category"] == "sin_datos"


def test_la_tolerancia_esta_lejos_del_ruido_y_del_bug():
    """Ni un ⚠ diario por ruido, ni un agujero que pase.

    Ruido medido con el cálculo corregido: ~$1.600 (los kg en máquinas se
    valúan a la apertura). El bug: ~$63.700.
    """
    assert 1600 * 3 < hv._HILADO_UKG_TOL_US < 63604 / 3
