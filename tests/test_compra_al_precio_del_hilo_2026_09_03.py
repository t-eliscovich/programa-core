"""Una compra de hilo sin importación entra al $/kg del hilado si se la marca.

TMT 2026-09-03 (dueña): pasó a compra un anticipo de MH del 2024 (21.253) y la
utilidad bajó eso: el $/kg del hilado sólo lo mueven las importaciones que
Asinfo marca recibidas en el mes y las compras locales, y esa compra no cruza
con nada. *"pasalo como una compra sin cruzar o algo así y cambia el precio
del hilo"* → marca `al_precio_hilo` (mig 0241) desde la ficha de la compra;
`mov_hilado_valuacion` suma su importe al promedio ponderado del mes.
"""
from __future__ import annotations

import contextlib
import os
import sys
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import service as sv  # noqa: E402
from modules.compras import queries as cq  # noqa: E402
from modules.importaciones import service as isv  # noqa: E402

HI0, OPEN = 1_960_000.0, 3.0437
INV_INIC = {"hilo": HI0}
INV_ACT = {"hilo": 1_900_000.0, "en_proceso_tc": 62_000.0}


def _valuacion(al_precio):
    rec = {"us": 60_573.72, "kg": 19_812.48, "kg_con_costo": 19_812.48, "usd_kg": None}
    return (
        patch.object(sv, "inventario_por_etapa_a_fecha", return_value=INV_INIC),
        patch.object(sv, "inventario_por_etapa", return_value=INV_ACT),
        patch.object(sv, "hilado_recibido_mes", return_value=19_812.48),
        patch.object(isv, "costo_hilado_recibido_mes", return_value=rec),
        patch("modules.compras_locales.service.hilado_local_recibido_mes",
              return_value={"kg": 0.0, "us": 0.0}),
        patch.object(cq, "hilo_al_precio_mes", return_value=al_precio),
    )


def _stock(al_precio):
    ps = _valuacion(al_precio)
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        return sv.mov_hilado_valuacion(2026, 9, OPEN)


def test_la_compra_marcada_sube_el_stock_casi_lo_que_vale():
    sin = _stock({"us": 0.0, "kg": 0.0, "n": 0})
    con = _stock({"us": 21_253.0, "kg": 0.0, "n": 1})
    salto = con["stock_act_us"] - sin["stock_act_us"]
    # 21.253 × hi1 / (hi0 + compras) — el stock actual pesa casi como la base.
    assert salto == pytest.approx(21_253.0 * INV_ACT["hilo"] / (HI0 + 19_812.48), rel=1e-6)
    assert 20_000 < salto < 21_253
    assert con["compras_us"] == pytest.approx(60_573.72 + 21_253.0)
    assert con["stock_act_ukg"] > sin["stock_act_ukg"]


def test_plata_sin_kilos_no_es_asimetria():
    """La marca trae dólares sin kg a propósito: no congela la tarifa."""
    con = _stock({"us": 21_253.0, "kg": 0.0, "n": 1})
    assert con["asimetrico"] is False
    assert con["tarifa_congelada"] is False


def test_si_la_consulta_falla_la_valuacion_sigue():
    ps = _valuacion(None)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(cq, "hilo_al_precio_mes", side_effect=RuntimeError("sin base")):
            out = sv.mov_hilado_valuacion(2026, 9, OPEN)
    assert out["disponible"] is True
    assert out["compras_us"] == pytest.approx(60_573.72)


# ── la marca ────────────────────────────────────────────────────────────────
class _DBStub:
    def __init__(self, compra):
        self.compra = compra
        self.executes: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        return dict(self.compra) if self.compra else None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


_MH = {"id_compra": 650, "numero": 10297, "fecha": "2026-09-03", "codigo_prov": "MH",
       "tipo": "H", "stat": None, "importe": 21253.0, "kg": None, "al_precio_hilo": False}


def _armar(monkeypatch, compra):
    import db
    import mov_doble
    s = _DBStub(compra)
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    s.regs = []
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: s.regs.append(kw) or 1)
    return s


def test_marcar_deja_rastro(monkeypatch):
    s = _armar(monkeypatch, _MH)
    r = cq.marcar_al_precio_hilo(650, marcar=True, usuario="tamara")
    assert r == {"cambio": True, "marcada": True, "numero": 10297}
    sql, params = s.executes[0]
    assert "update scintela.compra set al_precio_hilo = %s where id_compra = %s" in sql
    assert params == (True, 650)
    assert s.regs[0]["tipo"] == "compra_al_precio_hilo"
    assert s.regs[0]["importe"] == 21253.0
    assert s.regs[0]["metadata"]["mes"] == "2026-09"


def test_desmarcar(monkeypatch):
    s = _armar(monkeypatch, dict(_MH, al_precio_hilo=True))
    r = cq.marcar_al_precio_hilo(650, marcar=False)
    assert r["cambio"] is True and r["marcada"] is False
    assert s.executes[0][1] == (False, 650)
    assert s.regs[0]["tipo"] == "compra_al_precio_hilo_reverso"


def test_ya_estaba_asi_no_toca(monkeypatch):
    s = _armar(monkeypatch, dict(_MH, al_precio_hilo=True))
    assert cq.marcar_al_precio_hilo(650, marcar=True)["cambio"] is False
    assert not s.executes and not s.regs


@pytest.mark.parametrize("cambio, msg", [
    ({"tipo": "Q"}, "tipo H"),
    ({"stat": "Y"}, "anulada"),
])
def test_guards(monkeypatch, cambio, msg):
    s = _armar(monkeypatch, dict(_MH, **cambio))
    with pytest.raises(ValueError, match=msg):
        cq.marcar_al_precio_hilo(650, marcar=True)
    assert not s.executes
