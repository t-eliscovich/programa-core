"""Tests de retenciones desde Asinfo: traer (service) + aplicar/deshacer.

TMT 2026-07-09 (dueña): traer la retención total (IVA+Fuente) de cada factura
desde Asinfo y aplicarla a las facturas de PC (registra scintela.retencion +
baja el saldo). Idempotente y reversible.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ───────────────────────── service.retenciones_periodo ─────────────────────

def test_retenciones_periodo_shape(monkeypatch):
    from modules.asinfo import service as svc
    rows = [
        {"numero": "001-099-000179161", "ret_fuente": 27.01, "ret_iva": 0, "ret_total": 27.01},
        {"numero": "001-099-000178966", "ret_fuente": 100.0, "ret_iva": 100.16, "ret_total": 200.16},
        {"numero": "", "ret_fuente": 5, "ret_iva": 0, "ret_total": 5},  # sin numero → se ignora
    ]
    captured = {}

    def _fake_fetch(card_id, params=None):
        captured["card_id"] = card_id
        captured["params"] = params
        return rows
    monkeypatch.setattr(svc.metabase_client, "fetch_card", _fake_fetch)
    out = svc.retenciones_periodo("2026-07-01", "2026-07-08")
    assert set(out) == {"001-099-000179161", "001-099-000178966"}
    assert out["001-099-000178966"]["ret_total"] == 200.16
    # default card 202 cuando no hay env
    monkeypatch.delenv("ASINFO_CARD_RETENCIONES", raising=False)
    svc.retenciones_periodo("2026-07-01", "2026-07-08")
    assert captured["card_id"] == "202"
    # params fecha_inicio/fecha_fin
    slugs = {p["target"][1][1] for p in captured["params"]}
    assert slugs == {"fecha_inicio", "fecha_fin"}


@pytest.fixture(autouse=True)
def _sin_cache_retenciones():
    """El cache TTL de retenciones (2026-07-18) no debe cruzar tests."""
    from modules.asinfo import service as _svc
    _svc.reset_retenciones_cache()
    yield
    _svc.reset_retenciones_cache()


def test_retenciones_periodo_env_override(monkeypatch):
    from modules.asinfo import service as svc
    monkeypatch.setenv("ASINFO_CARD_RETENCIONES", "999")
    cap = {}

    def _f(cid, params=None):
        cap["id"] = cid
        return []
    monkeypatch.setattr(svc.metabase_client, "fetch_card", _f)
    svc.retenciones_periodo("2026-07-01", "2026-07-08")
    assert cap["id"] == "999"


# ───────────────────────── _aplicar_una_por_numero ─────────────────────────

class _DBStub:
    def __init__(self, factura, existing_ret=False, mov=None):
        self.factura = factura
        self.existing_ret = existing_ret
        self.mov = mov
        self.updates = []
        self.deletes = []
        self.inserts_ret = []
        self.mov_dobles = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura) if self.factura else None
        if "from scintela.retencion" in s:
            return {"x": 1} if self.existing_ret else None
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        return None

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates.append(tuple(params))
        elif "delete from scintela.retencion" in s:
            self.deletes.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "insert into scintela.retencion" in s:
            self.inserts_ret.append(tuple(params))
            return {"id_retencion": 55}
        if "insert into scintela.mov_doble" in s:
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 77}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch(monkeypatch, stub):
    import db
    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "execute_returning", stub.execute_returning)
    monkeypatch.setattr(db, "tx", stub.tx)


def _fac(importe=100.0, abono=0.0, saldo=100.0, stat="Z"):
    return {"id_factura": 7, "codigo_cli": "EDU", "numf": 1234,
            "numf_completo": "001-099-000179161", "importe": importe,
            "abono": abono, "saldo": saldo, "stat": stat}


def test_aplicar_una_aplica(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 0.0, 100.0, "Z"))
    _patch(monkeypatch, stub)
    r = q._aplicar_una_por_numero("001-099-000179161", 27.01, "tester")
    assert r == "aplicada"
    # retencion insertada con rete
    assert stub.inserts_ret[0][2] == 27.01
    # factura: abono=27.01, saldo=72.99, stat A
    upd = stub.updates[0]
    assert upd[0] == 27.01 and upd[1] == 72.99 and upd[2] == "A"
    # mov_doble registrado
    assert stub.mov_dobles[0][1] == "retencion_asinfo_aplicada"


def test_aplicar_una_cierra_T(monkeypatch):
    from modules.retenciones import queries as q
    # rete == saldo → saldo 0 → T
    stub = _DBStub(_fac(100.0, 0.0, 100.0, "Z"))
    _patch(monkeypatch, stub)
    r = q._aplicar_una_por_numero("001-099-000179161", 100.0, "t")
    assert r == "aplicada"
    assert stub.updates[0][1] == 0.0 and stub.updates[0][2] == "T"


def test_aplicar_una_idempotente(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(), existing_ret=True)
    _patch(monkeypatch, stub)
    assert q._aplicar_una_por_numero("001-099-000179161", 27.01, "t") == "ya"
    assert not stub.updates and not stub.inserts_ret


def test_aplicar_una_sin_factura(monkeypatch):
    from modules.retenciones import queries as q
    _patch(monkeypatch, _DBStub(None))
    assert q._aplicar_una_por_numero("X", 27.01, "t") == "sin_factura"


def test_aplicar_una_rete_0(monkeypatch):
    from modules.retenciones import queries as q
    _patch(monkeypatch, _DBStub(_fac()))
    assert q._aplicar_una_por_numero("X", 0.0, "t") == "rete_0"


def test_aplicar_una_rete_gt_importe(monkeypatch):
    from modules.retenciones import queries as q
    _patch(monkeypatch, _DBStub(_fac(100.0)))
    assert q._aplicar_una_por_numero("X", 150.0, "t") == "rete_gt_importe"


# ───────────────────────── aplicar_retenciones_asinfo (lote) ───────────────

def test_lote_tally(monkeypatch):
    from modules.asinfo import service as svc
    from modules.retenciones import queries as q
    monkeypatch.setattr(svc, "retenciones_periodo", lambda d, h: {
        "A": {"ret_total": 10.0}, "B": {"ret_total": 20.0},
        "C": {"ret_total": 5.0}, "D": {"ret_total": 7.0},
    })
    outcomes = {"A": "aplicada", "B": "aplicada", "C": "ya", "D": "sin_factura"}
    monkeypatch.setattr(q, "_aplicar_una_por_numero",
                        lambda numero, rete, usuario, batch_id=None: outcomes[numero])
    r = q.aplicar_retenciones_asinfo("2026-07-01", "2026-07-08", usuario="t")
    assert r["n_aplicadas"] == 2 and r["total_aplicado"] == 30.0
    assert r["n_ya"] == 1 and r["n_sin_factura"] == 1
    assert r["n_retenciones_asinfo"] == 4


def test_lote_error_no_rompe(monkeypatch):
    from modules.asinfo import service as svc
    from modules.retenciones import queries as q
    monkeypatch.setattr(svc, "retenciones_periodo", lambda d, h: {"A": {"ret_total": 10.0}})

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(q, "_aplicar_una_por_numero", _boom)
    r = q.aplicar_retenciones_asinfo("2026-07-01", "2026-07-08")
    assert r["n_error"] == 1 and r["n_aplicadas"] == 0


# ───────────────────────── desaplicar (reverso) ────────────────────────────

def test_desaplicar_una(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(
        _fac(100.0, 27.01, 72.99, "A"),
        mov={"id_mov_doble": 88,
             "metadata": {"id_retencion": 55, "rete": 27.01,
                          "abono_previo": 0.0, "saldo_previo": 100.0,
                          "stat_previo": "Z"}},
    )
    _patch(monkeypatch, stub)
    r = q._desaplicar_una_por_numero("001-099-000179161", "t")
    assert r == "revertida"
    # restaura saldo 100, abono 0, stat Z
    assert stub.updates[0][0] == 0.0 and stub.updates[0][1] == 100.0 and stub.updates[0][2] == "Z"
    # borra la retencion 55
    assert stub.deletes and stub.deletes[0][0] == 55
    # reverso mov_doble
    assert stub.mov_dobles[0][1] == "retencion_asinfo_desaplicada"


def test_desaplicar_una_sin_aplicacion(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(), mov=None)
    _patch(monkeypatch, stub)
    assert q._desaplicar_una_por_numero("X", "t") == "sin_aplicacion"


# ───────────────────────── cron helper (health/all) ────────────────────────

def test_cron_helper_ok(monkeypatch):
    from modules.admin_dbase import health_audit_view as hv
    from modules.retenciones import queries as q
    monkeypatch.setattr(q, "aplicar_retenciones_asinfo",
                        lambda d, h, usuario="cron": {"n_aplicadas": 3, "total_aplicado": 90.0})
    r = hv._aplicar_retenciones_asinfo_cron(dias=60)
    assert r["ok"] is True and r["n_aplicadas"] == 3


def test_cron_helper_failsoft(monkeypatch):
    from modules.admin_dbase import health_audit_view as hv
    from modules.retenciones import queries as q

    def _boom(*a, **k):
        raise RuntimeError("metabase down")
    monkeypatch.setattr(q, "aplicar_retenciones_asinfo", _boom)
    r = hv._aplicar_retenciones_asinfo_cron()
    assert r["ok"] is False and "metabase down" in r["error"]
