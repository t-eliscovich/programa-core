"""La OP 100352 borrada con el tacho, y la vuelta atrás desde el historial.

TMT 2026-10-01. Andrés pagó la OP 100335 y borró la 100352, que tenía
37.431,04 sin retirar. La utilidad bajó 37 mil sin que se moviera un peso.

Se clava:
  - una OP con más de 1 dólar sin retirar NO se borra (hay que usar «Retirar»);
  - con centavos sí, y cualquier otro proveedor sigue igual;
  - «deshacer» desde el historial revive el posdatado tal como estaba, deja el
    reverso en el historial y no actúa dos veces.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    def __init__(self, posdat=None, mov=None):
        self.posdat, self.mov = posdat, mov
        self.sql: list[tuple[str, tuple]] = []

    @staticmethod
    def _n(sql):
        return " ".join((sql or "").split()).lower()

    def fetch_one(self, sql, params=None, conn=None):
        s = self._n(sql)
        if "from scintela.mov_doble" in s:
            if "where id_mov_doble" in s:
                return dict(self.mov) if self.mov else None
            return None
        if "from scintela.posdat" in s:
            return dict(self.posdat) if self.posdat else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.sql.append((self._n(sql), tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.sql.append((self._n(sql), tuple(params or ())))
        return {"id_mov_doble": 999}

    def escribio(self, needle):
        return [x for x in self.sql if needle in x[0]]


@pytest.fixture
def fake(monkeypatch):
    def _go(**kw):
        import db as db_mod
        f = _FakeDB(**kw)
        for n in ("fetch_one", "fetch_all", "execute", "execute_returning"):
            monkeypatch.setattr(db_mod, n, getattr(f, n))

        @contextlib.contextmanager
        def _tx(*a, **k):
            yield object()
        monkeypatch.setattr(db_mod, "tx", _tx)
        return f
    return _go


def _op(importe, prov="OP"):
    return {"id_posdat": 1183, "num": 100352, "prov": prov, "importe": importe,
            "banc": 0, "fecha": None, "anulada": False,
            "concepto": "AI 35/38/46/47 [CM]"}


def test_op_con_saldo_no_se_borra(fake):
    from modules.posdat import queries
    f = fake(posdat=_op(-37431.04))
    with pytest.raises(ValueError) as e:
        queries.anular(1183, motivo="op pagado proveedor", usuario="andres")
    assert "37.431,04" in str(e.value) and "Retirar" in str(e.value)
    assert not f.escribio("update scintela.posdat")


def test_op_con_centavos_se_borra(fake):
    from modules.posdat import queries
    f = fake(posdat=_op(-0.97))
    queries.anular(998, motivo="OP PAGADO POR PROVEEDOR", usuario="andres")
    assert f.escribio("set anulada = true")


def test_op_en_cero_se_borra(fake):
    from modules.posdat import queries
    f = fake(posdat=_op(0))
    queries.anular(1055, motivo="OP PAGADO POR PROVEEDOR", usuario="andres")
    assert f.escribio("set anulada = true")


def test_otro_proveedor_no_cambia(fake):
    from modules.posdat import queries
    f = fake(posdat=_op(15000, prov="MH"))
    queries.anular(784, motivo="Eliminado desde panel", usuario="tamara")
    assert f.escribio("set anulada = true")


def _mov(**kw):
    m = {"id_mov_doble": 43274, "tipo": "posdat_anulada", "estado": "activo",
         "origen_id": 1183, "id_original": None}
    m.update(kw)
    return m


def test_deshacer_revive_el_posdatado(fake):
    from modules.posdat import queries
    pd = _op(-37431.04)
    pd["anulada"] = True
    f = fake(posdat=pd, mov=_mov())
    res = queries.deshacer_anulacion(43274, usuario="tamara")
    upd = f.escribio("set anulada = false")
    assert upd and upd[0][1][-1] == 1183
    ins = f.escribio("insert into scintela.mov_doble")
    assert ins and "reverso_posdat_anulada" in ins[0][1]
    # la anulación queda reversada (no se puede deshacer dos veces)
    assert any(p[-1] == 43274 for _, p in f.escribio("set estado='reversado'"))
    assert res["importe"] == -37431.04 and res["num"] == 100352


def test_deshacer_devuelve_a_activo_el_movimiento_que_la_anulacion_marco(fake):
    from modules.posdat import queries
    pd = _op(-55.77)
    pd["anulada"] = True
    f = fake(posdat=pd, mov=_mov(estado="reverso", id_original=26705))
    queries.deshacer_anulacion(43274, usuario="tamara")
    back = f.escribio("set estado = 'activo', id_reverso = null")
    assert back and back[0][1] == (26705, 43274)


@pytest.mark.parametrize("mov,anulada,msg", [
    (_mov(tipo="retiro_op"), True, "no es la eliminación"),
    (_mov(estado="reversado"), True, "ya se deshizo"),
    (_mov(), False, "ya está vivo"),
])
def test_deshacer_frena(fake, mov, anulada, msg):
    from modules.posdat import queries
    pd = _op(-37431.04)
    pd["anulada"] = anulada
    f = fake(posdat=pd, mov=mov)
    with pytest.raises(ValueError) as e:
        queries.deshacer_anulacion(43274, usuario="tamara")
    assert msg in str(e.value)
    assert not f.escribio("update scintela.posdat")


def test_historial_lo_deshace_inline_con_el_permiso_de_borrar():
    from modules.historial import views
    assert views._PERMISO_REVERSO_INLINE["posdat_anulada"] == "posdat.anular"
    from modules.historial import queries as hq
    assert "deshizo" in hq.label("reverso_posdat_anulada")
