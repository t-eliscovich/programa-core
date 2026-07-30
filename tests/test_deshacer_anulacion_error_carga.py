"""Deshacer una anulación por "error de carga".

TMT 2026-07-30 (dueña: *"tiene que tener pantalla"*). Era el único movimiento
del módulo de cheques sin vuelta atrás: /historial mostraba el ↺ y el
dispatcher contestaba "aún no tiene reverso automatizado". El caso testigo es
el anticipo de CJE #100934 (1.700), que hubo que RE-CREAR entero por esto —
con el ruido que eso deja en la conciliación del banco.

Lo que se clava acá es lo que puede salir caro: que no se deshaga dos veces,
que no pise un cheque que alguien movió después, y que la compensación se
revierta con su OPUESTA en vez de borrar la fila.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    def __init__(self, mov, cheque):
        self.mov, self.cheque = mov, cheque
        self.sql: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        if "from scintela.cheque" in s:
            return dict(self.cheque) if self.cheque else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.sql.append((" ".join((sql or "").split()).lower(), tuple(params or ())))
        return 1

    def escribio(self, needle):
        return [x for x in self.sql if needle in x[0]]


def _mov(*, estado="reverso", stat_previo="B", comp=None, snap=None,
         n_aplic=0, tipo="reverso_cheque_administrativo"):
    meta = {"id_cheque": 501, "stat_previo": stat_previo,
            "n_aplicaciones_reversadas": n_aplic, "compensacion": comp,
            "motivo": "importe mal"}
    if snap is not None:
        meta["aplicaciones_borradas"] = snap
    return {"id_mov_doble": 900, "tipo": tipo, "origen_id": 501,
            "estado": estado, "metadata": meta, "importe": 1700.0}


def _ch(stat="X", importe=1700.0, no_banco=90):
    return {"id_cheque": 501, "no_cheque": "", "stat": stat,
            "codigo_cli": "CJE", "importe": importe, "no_banco": no_banco}


@pytest.fixture
def _run(monkeypatch):
    def _go(mov, ch, *, bank=None, caja=None, aplicar=None):
        import bank_helpers
        import caja_helpers
        import db as db_mod
        import mov_doble
        from modules.cheques import queries as q
        fake = _FakeDB(mov, ch)
        for n, f in (("fetch_one", fake.fetch_one), ("fetch_all", fake.fetch_all),
                     ("execute", fake.execute)):
            monkeypatch.setattr(db_mod, n, f)

        @contextlib.contextmanager
        def _tx(*a, **k):
            yield object()
        monkeypatch.setattr(db_mod, "tx", _tx)
        monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None)
        monkeypatch.setattr(q, "today_ec", lambda: date(2026, 7, 30))
        monkeypatch.setattr(mov_doble, "registrar", lambda **kw: 1)
        monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                            lambda *a, **kw: (bank.append(kw) if bank is not None else None)
                            or {"id_transaccion": 7})
        monkeypatch.setattr(caja_helpers, "insert_movimiento_caja",
                            lambda *a, **kw: (caja.append(kw) if caja is not None else None)
                            or {"id_caja": 8})
        monkeypatch.setattr(q, "aplicar_a_factura",
                            lambda **kw: (aplicar.append(kw) if aplicar is not None else None))
        return q, fake
    return _go


def test_devuelve_el_cheque_a_su_stat(_run):
    q, fake = _run(_mov(), _ch())
    res = q.deshacer_anulacion_error_carga(900, usuario="tamara")
    assert res["stat_restaurado"] == "B"
    upd = fake.escribio("update scintela.cheque")
    assert len(upd) == 1 and upd[0][1][0] == "B"


def test_no_se_deshace_dos_veces(_run):
    q, fake = _run(_mov(estado="reversado"), _ch())
    with pytest.raises(ValueError, match="ya se deshizo"):
        q.deshacer_anulacion_error_carga(900)
    assert not fake.escribio("update scintela.cheque")


def test_no_pisa_un_cheque_que_alguien_movio(_run):
    q, fake = _run(_mov(), _ch(stat="Z"))
    with pytest.raises(ValueError, match="stat='Z'"):
        q.deshacer_anulacion_error_carga(900)
    assert not fake.escribio("update scintela.cheque")


def test_rechaza_otro_tipo_de_movimiento(_run):
    q, _ = _run(_mov(tipo="cheque_creado"), _ch())
    with pytest.raises(ValueError, match="no una anulación"):
        q.deshacer_anulacion_error_carga(900)


def test_sin_stat_previo_no_adivina(_run):
    q, fake = _run(_mov(stat_previo=""), _ch())
    with pytest.raises(ValueError, match="de qué estado venía"):
        q.deshacer_anulacion_error_carga(900)
    assert not fake.escribio("update scintela.cheque")


def test_la_nd_se_compensa_con_una_nc(_run):
    """No se borra la fila del banco: se mete la opuesta. El paper trail
    entero, igual que el resto de los reversos de la app."""
    bank: list[dict] = []
    q, _ = _run(_mov(comp={"tipo": "banco", "id": 42}), _ch(), bank=bank)
    q.deshacer_anulacion_error_carga(900)
    assert len(bank) == 1
    assert bank[0]["documento"] == "NC", "la ND se compensa con una NC"
    assert bank[0]["importe"] == 1700.0
    assert bank[0]["numreferencia"] == 501


def test_la_salida_de_caja_vuelve_como_entrada(_run):
    caja: list[dict] = []
    q, _ = _run(_mov(comp={"tipo": "caja", "id": 42}, stat_previo="C"),
                _ch(no_banco=99), caja=caja)
    q.deshacer_anulacion_error_carga(900)
    assert len(caja) == 1 and caja[0]["tipo"] == "E"


def test_un_efectivo_negativo_vuelve_como_salida(_run):
    """La anulación de un efectivo NEGATIVO había PUESTO plata en caja ('E');
    deshacerla tiene que sacarla."""
    caja: list[dict] = []
    q, _ = _run(_mov(comp={"tipo": "caja", "id": 42}, stat_previo="C"),
                _ch(importe=-1679.08, no_banco=99), caja=caja)
    q.deshacer_anulacion_error_carga(900)
    assert caja[0]["tipo"] == "S" and caja[0]["importe"] == pytest.approx(1679.08)


def test_reaplica_las_facturas_del_snapshot(_run):
    aplic: list[dict] = []
    q, _ = _run(_mov(snap=[{"id_fact": 11, "importe": 900.0},
                           {"id_fact": 12, "importe": 800.0}], n_aplic=2),
                _ch(), aplicar=aplic)
    res = q.deshacer_anulacion_error_carga(900)
    assert res["aplicaciones_reaplicadas"] == 2
    assert res["aplicaciones_pendientes"] == 0
    assert len(aplic) == 1 and len(aplic[0]["aplicaciones"]) == 2


def test_anulacion_vieja_avisa_lo_que_no_puede_reaplicar(_run):
    """Sin snapshot (anulaciones anteriores al 30/07) el cheque vuelve igual,
    pero hay que decir cuántas facturas quedaron sin re-aplicar en vez de
    dejar el hueco callado."""
    aplic: list[dict] = []
    q, _ = _run(_mov(n_aplic=3), _ch(), aplicar=aplic)
    res = q.deshacer_anulacion_error_carga(900)
    assert res["aplicaciones_reaplicadas"] == 0
    assert res["aplicaciones_pendientes"] == 3
    assert aplic == [], "no inventa aplicaciones"
