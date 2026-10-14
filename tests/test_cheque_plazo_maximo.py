"""Un cheque no se deposita a más de 360 días de haber entrado.

Tamara 13/10/2026: *"que no se pueda pagar a más de 360"*. Salió del cheque
3616 de GUG, cargado el 13/10/2026 a depositar el 31/12/2027 (444 días: el
año tipeado de más).
"""
from datetime import date
from pathlib import Path

import pytest

from modules.cheques import queries as q


def test_hasta_360_dias_pasa():
    q.validar_plazo(date(2027, 10, 8), date(2026, 10, 13))   # 360 justos


def test_361_dias_frena_y_dice_cuanto():
    with pytest.raises(ValueError, match="361 días"):
        q.validar_plazo(date(2027, 10, 9), date(2026, 10, 13))


def test_el_caso_gug_3616_frena():
    with pytest.raises(ValueError, match="31/12/2027"):
        q.validar_plazo(date(2027, 12, 31), date(2026, 10, 13))


def test_sin_fechas_no_frena():
    q.validar_plazo(None, date(2026, 10, 13))
    q.validar_plazo(date(2027, 12, 31), None)


def test_el_alta_la_edicion_y_postergar_usan_el_freno():
    src = (Path(__file__).resolve().parents[1] / "modules/cheques/queries.py").read_text(
        encoding="utf-8")
    assert src.count("validar_plazo(") >= 5   # def + alta + edición + postergar + protestado
    i = src.index("    fechad = _domingo_a_lunes(fechad)\n    fecha_recibido")
    assert "validar_plazo(fechad, fecha_recibido)" in src[i:i + 200]
