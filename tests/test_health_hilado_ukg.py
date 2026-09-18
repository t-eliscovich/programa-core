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


def _foto(ukg, hil_kg=HILADO_KG, com_kg=COMPRAS_KG, com_us=COMPRAS_US,
          insumos=None):
    return {"creado_en": "2026-08-07T11:35:00+00:00", "hilado_kg": hil_kg,
            "hilado_ukg": ukg, "compras_kg": com_kg, "compras_us": com_us,
            "kg_sin_costo": 0.0, "hilado_insumos": insumos}


# Producción, 18/09/2026 12:55 — la foto que hizo sonar el health con la
# apertura PERFECTA (3,043548 = 6.009.239 / 1.974.421 del PDF de agosto).
SEP_APERTURA = 3.043548
SEP_HI0 = 1933359.0         # hilo en Asinfo al 01/09
SEP_MAQ = 40566.0           # en máquinas
SEP_HILADO_KG = 1960064.21  # hi1 + maq
SEP_HI1 = SEP_HILADO_KG - SEP_MAQ
SEP_COMPRAS_KG = 181253.76
SEP_COMPRAS_US = 663472.10  # 3,66 US$/kg: 0,62 arriba de la apertura
SEP_UKG = 3.0953            # lo que mostraba el balance (y era correcto)
SEP_INSUMOS = {"hi0": SEP_HI0, "hi1": SEP_HI1, "maq": SEP_MAQ,
               "open_ukg": SEP_APERTURA, "tarifa_congelada": False}


def _foto_sep(ukg=SEP_UKG, insumos=SEP_INSUMOS):
    return _foto(ukg, hil_kg=SEP_HILADO_KG, com_kg=SEP_COMPRAS_KG,
                 com_us=SEP_COMPRAS_US, insumos=insumos)


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
    assert abs(out["stats"]["gap_us"]) < out["stats"]["tolerancia_us"] == hv._HILADO_UKG_TOL_US_SIN_KILOS


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

    Con la cuenta exacta el ruido es el redondeo del $/kg a 4 decimales en
    la foto (~100 US$ sobre 2 millones de kg). El bug de agosto: ~$63.700.
    Para las fotos viejas (sin kilos) queda la tolerancia vieja: ~$1.600 de
    ruido medido el 07/08 con compras cerca de la apertura.
    """
    assert 100 * 3 < hv._HILADO_UKG_TOL_US < 63604 / 10
    assert 1600 * 3 < hv._HILADO_UKG_TOL_US_SIN_KILOS < 63604 / 3


# ── 18/09/2026: el falso positivo del sobreprecio ────────────────────────


def test_el_18_09_con_la_cuenta_vieja_sonaba_por_el_sobreprecio():
    """La foto sin kilos cae a la cuenta vieja y da los −10.380 de ese día:
    el sobreprecio de las compras que se fue con los 195.115 kg de egresos."""
    out = _correr(_foto_sep(insumos=None), apertura=SEP_APERTURA)
    assert out["stats"]["con_kilos"] is False
    assert round(out["stats"]["gap_us"], 0) == -10380.0
    assert out["ok"] is False


def test_el_18_09_con_los_kilos_de_la_foto_se_reconstruye_al_centavo():
    """Con hi0/hi1/maq el health repite la cuenta de mov_hilado_valuacion y
    el 3,0953 cierra: la apertura estaba bien y no había nada que arreglar."""
    out = _correr(_foto_sep(), apertura=SEP_APERTURA)
    assert out["ok"] is True and out["alerts"] == []
    assert out["stats"]["con_kilos"] is True
    assert round(out["stats"]["ukg_esperado"], 4) == 3.0953
    assert abs(out["stats"]["gap_us"]) < 200
    assert out["stats"]["hi0"] == SEP_HI0 and out["stats"]["maq"] == SEP_MAQ


def test_con_los_kilos_una_apertura_recalculada_si_suena():
    """El bug de agosto (apertura = cierre del mes en curso, las compras
    diluidas dos veces) sigue sonando fuerte con la cuenta nueva."""
    out = _correr(_foto_sep(ukg=3.1253), apertura=SEP_APERTURA)
    assert out["ok"] is False
    assert out["alerts"][0]["category"] == "hilado_ukg_no_reconstruible"
    assert out["stats"]["gap_us"] > 50000


def test_hilado_insumos_como_texto_tambien_sirve():
    import json
    out = _correr(_foto_sep(insumos=json.dumps(SEP_INSUMOS)), apertura=SEP_APERTURA)
    assert out["ok"] is True and out["stats"]["con_kilos"] is True


def test_tarifa_congelada_no_se_reconstruye():
    """Si el $/kg se sostuvo porque Asinfo no contestó, no se arma con estos
    insumos: aviso bajo, sin alarma."""
    ins = dict(SEP_INSUMOS, tarifa_congelada=True)
    out = _correr(_foto_sep(ukg=3.0500, insumos=ins), apertura=SEP_APERTURA)
    assert out["ok"] is True
    assert [a["category"] for a in out["alerts"]] == ["tarifa_congelada"]
