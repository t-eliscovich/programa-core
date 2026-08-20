"""Un cheque cuyos vínculos borró un TOTALIZAR no se anula a ciegas.

TMT 2026-08-20. `anular_por_error_de_carga` y la anulación administrativa de
`reversar` arman qué desabonar leyendo `scintela.chequesxfact`, y
`totalizar_estado_cuenta_ejecutar` borra esa tabla a propósito. Juntos: la
compensación entra al banco y el abono se queda puesto en la factura.

Medido en producción el 20/08: 114 cheques por $103.002,62 en 10 clientes
están en ese estado, los 114 explicados por un totalizar del mismo cliente.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DB:
    """Stub de `db.fetch_one` para el detector. `huerfano` = sin vínculos."""

    def __init__(self, huerfano: bool, cuando=None):
        self.huerfano = huerfano
        self.cuando = cuando
        self.consultas: list[str] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        self.consultas.append(s)
        if not self.huerfano:
            return None
        return {"no_cheque": "  99366 ", "codigo_cli": "MTM",
                "totalizado_el": self.cuando}


def _freno(monkeypatch, huerfano, cuando=None):
    import db
    from modules.cheques import queries as q
    stub = _DB(huerfano, cuando)
    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    return q._freno_si_el_totalizar_se_llevo_los_vinculos, stub


def test_deja_pasar_al_cheque_que_conserva_sus_vinculos(monkeypatch):
    fn, _ = _freno(monkeypatch, huerfano=False)
    fn(123)   # no levanta


def test_frena_al_cheque_que_perdio_los_vinculos(monkeypatch):
    from datetime import datetime
    fn, _ = _freno(monkeypatch, huerfano=True, cuando=datetime(2026, 8, 3, 12, 25))
    with pytest.raises(ValueError) as e:
        fn(123)
    msg = str(e.value)
    assert "99366" in msg and "MTM" in msg
    assert "03/08/2026" in msg              # cuándo se totalizó
    assert "Historial" in msg               # la salida
    assert "a mano" in msg                  # la otra salida


def test_sin_fecha_de_totalizar_igual_frena_y_no_explota(monkeypatch):
    fn, _ = _freno(monkeypatch, huerfano=True, cuando=None)
    with pytest.raises(ValueError) as e:
        fn(123)
    assert "(" not in str(e.value).split("estado de cuenta")[1][:3]


def test_el_detector_pide_aplicacion_activa_y_cero_vinculos(monkeypatch):
    """La consulta tiene que exigir las dos condiciones, no una."""
    fn, stub = _freno(monkeypatch, huerfano=False)
    fn(123)
    s = stub.consultas[0]
    assert "cheque_aplicado_a_factura" in s
    assert "estado = 'activo'" in s
    assert "not exists" in s and "chequesxfact" in s


def test_el_rebote_real_no_se_frena():
    """B→1, V→2, 1/2→3 no tocan la factura: frenarlos sería absurdo.

    Se comprueba sobre la máquina de estados, que es la que decide.
    """
    from modules.cheques.queries import _stat_destino_reversa
    for stat in ("B", "V", "1", "2", "A"):
        _destino, es_rebote_real = _stat_destino_reversa(stat)
        assert es_rebote_real, stat
    for stat in ("Z", "D", "P"):
        destino, es_rebote_real = _stat_destino_reversa(stat)
        assert not es_rebote_real and destino == "X", stat


def test_los_dos_caminos_administrativos_llaman_al_freno():
    """Si alguien agrega un tercero, que este test lo obligue a mirarlo."""
    import inspect

    from modules.cheques import queries as q
    for fn in (q.anular_por_error_de_carga, q.reversar):
        src = inspect.getsource(fn)
        assert "_freno_si_el_totalizar_se_llevo_los_vinculos" in src, fn.__name__
    # y en reversar tiene que estar detrás del `if not es_rebote_real`
    src = inspect.getsource(q.reversar)
    i = src.index("_freno_si_el_totalizar_se_llevo_los_vinculos")
    assert "if not es_rebote_real:" in src[max(0, i - 200):i]
