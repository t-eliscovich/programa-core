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
        self.updates_sql = []
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
            self.updates_sql.append(sql)
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


def _fac(importe=100.0, abono=0.0, saldo=100.0, stat="Z", retencion=0.0):
    return {"id_factura": 7, "codigo_cli": "EDU", "numf": 1234,
            "numf_completo": "001-099-000179161", "importe": importe,
            "abono": abono, "retencion": retencion, "saldo": saldo,
            "stat": stat}


def _sql_updates_factura(stub):
    """Los UPDATE de scintela.factura, con su SQL — para poder afirmar qué
    columnas TOCA (y cuáles no)."""
    return [s for s, _p in getattr(stub, "updates_sql", [])]


def test_aplicar_una_aplica(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 0.0, 100.0, "Z"))
    _patch(monkeypatch, stub)
    r = q._aplicar_una_por_numero("001-099-000179161", 27.01, "tester")
    assert r == "aplicada"
    # retencion insertada con rete
    assert stub.inserts_ret[0][2] == 27.01
    # TMT 2026-08-07: la retención va a factura.retencion, NO al abono, y la
    # factura NO pasa a 'A': nadie abonó nada, sólo debe menos. (Este test
    # pedía 'A' hasta esta fecha; se da vuelta, no se borra — ver
    # tests/test_retencion_stat_y_reverso_puntual.py.)
    upd = stub.updates[0]
    assert upd[0] == 27.01 and upd[1] == 72.99 and upd[2] == "Z"
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


def test_aplicar_una_no_toca_el_abono_previo(monkeypatch):
    """TMT 2026-08-07 (dueña): "dividir esto en dos columnas, retenciones y
    abono, no todo junto (entonces no se debería sumar más)".

    La retención sigue contando contra la deuda (eso lo decidió el 06/08 y no
    cambia), pero el abono manual queda intacto: 51,98 pagados + 27,01
    retenidos sobre 100 → saldo 21,01, y en la pantalla se leen los DOS
    números por separado en vez de un "abonado 78,99" que no es lo que el
    cliente pagó.

    Antes este test se llamaba `test_aplicar_una_suma_al_abono_previo` y
    exigía abono=78,99. Se da vuelta, no se borra: la regla vieja tiene que
    quedar fallando si alguien la reintroduce."""
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 51.98, 48.02, "A"))  # abono manual previo
    _patch(monkeypatch, stub)
    r = q._aplicar_una_por_numero("001-099-000179161", 27.01, "tester")
    assert r == "aplicada"
    # registró la retención (informe/SRI + guard anti-reaplicación)
    assert stub.inserts_ret and stub.inserts_ret[0][2] == 27.01
    # retención 27.01, saldo 100 − 51.98 − 27.01 = 21.01, sigue A
    upd = stub.updates[0]
    assert upd[0] == 27.01 and upd[1] == 21.01 and upd[2] == "A"
    # y el UPDATE no menciona `abono`: el abono manual no se toca nunca más.
    assert "abono" not in stub.updates_sql[0].lower()
    md = stub.mov_dobles[0]
    assert md[1] == "retencion_asinfo_aplicada"


def test_aplicar_una_suma_y_cierra_T(monkeypatch):
    """Abono previo + retención = importe → saldo 0, stat T."""
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 90.0, 10.0, "A"))
    _patch(monkeypatch, stub)
    r = q._aplicar_una_por_numero("001-099-000179161", 10.0, "t")
    assert r == "aplicada"
    # retención 10 (el abono sigue en 90, aparte) → saldo 0 → T
    assert stub.updates[0][0] == 10.0 and stub.updates[0][1] == 0.0
    assert stub.updates[0][2] == "T"


def test_aplicar_una_rete_gt_saldo(monkeypatch):
    """Freno 2026-08-06: si la retención dejaría el saldo negativo, no se
    aplica sola (va a n_error para mirarla a mano)."""
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 90.0, 10.0, "A"))
    _patch(monkeypatch, stub)
    assert q._aplicar_una_por_numero("001-099-000179161", 27.01, "t") == "rete_gt_saldo"
    assert not stub.updates and not stub.inserts_ret


def test_aplicar_una_ya_gana_a_rete_gt_saldo(monkeypatch):
    """Una retención YA aplicada bajó el saldo; al re-correr el cron, `ya`
    tiene que ganar antes del freno rete>saldo (si no, cada corrida la
    contaría como error)."""
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(100.0, 60.0, 40.0, "A"), existing_ret=True)
    _patch(monkeypatch, stub)
    assert q._aplicar_una_por_numero("001-099-000179161", 60.0, "t") == "ya"
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
                        lambda numero, rete, usuario, batch_id=None, solo_sin_abono=False: outcomes[numero])
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
    # restaura abono 0, retención 0, saldo 100, stat Z. El snapshot es VIEJO
    # (no trae `retencion_previa`): el reverso deduce "lo de antes" restando
    # la retención que saca — 27.01 − 27.01 = 0.
    assert stub.updates[0][0] == 0.0            # abono
    assert stub.updates[0][1] == 0.0            # retencion
    assert stub.updates[0][2] == 100.0          # saldo
    assert stub.updates[0][3] == "Z"            # stat
    # borra la retencion 55
    assert stub.deletes and stub.deletes[0][0] == 55
    # reverso mov_doble
    assert stub.mov_dobles[0][1] == "retencion_asinfo_desaplicada"


def test_desaplicar_una_sin_aplicacion(monkeypatch):
    from modules.retenciones import queries as q
    stub = _DBStub(_fac(), mov=None)
    _patch(monkeypatch, stub)
    assert q._desaplicar_una_por_numero("X", "t") == "sin_aplicacion"


# ───────────────────────── corregir doble retención ────────────────────────

def test_corregir_doble_solo_los_dobles(monkeypatch):
    """Cuenta SÓLO los mov_doble con abono_previo>0 (dobles). Los de
    abono_previo=0 (sanos, el dBase no los tenía) no entran.

    TMT 2026-08-07: desde la mig 0179 la ejecución REAL está bloqueada — la
    retención ya no vive adentro del abono, así que restarla de ahí dejaría el
    abono corto y el saldo inflado. Queda sólo el dry_run."""
    import db
    from modules.retenciones import queries as q

    movs = [
        {"id_mov_doble": 1, "origen_id": 100, "importe": 51.98,
         "metadata": {"abono_previo": 51.98, "rete": 51.98, "numf": 177206}},  # doble
        {"id_mov_doble": 2, "origen_id": 200, "importe": 10.0,
         "metadata": {"abono_previo": 0.0, "rete": 10.0, "numf": 179905}},  # sano
    ]
    facturas = {100: {"id_factura": 100, "importe": 2988.66, "abono": 103.96,
                      "saldo": 2884.70}}
    updates = []

    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: list(movs))

    def _fo(sql, params=None, conn=None):
        return dict(facturas.get(params[0])) if params and params[0] in facturas else None
    monkeypatch.setattr(db, "fetch_one", _fo)

    def _ex(sql, params=None, conn=None):
        if "update scintela.factura" in " ".join(sql.split()).lower():
            updates.append(tuple(params))
        return 1
    monkeypatch.setattr(db, "execute", _ex)

    import contextlib

    @contextlib.contextmanager
    def _tx():
        yield object()
    monkeypatch.setattr(db, "tx", _tx)
    monkeypatch.setattr(q._md, "registrar", lambda **k: {"id_mov_doble": 9})

    # cuenta sin mutar (es lo único que hace desde la mig 0179)
    prev = q.corregir_doble_retenciones(dry_run=True)
    assert prev["n_doble"] == 1 and prev["total_corregido"] == 51.98
    assert prev["n_skip_no_doble"] == 1
    assert not updates

    # pedirle que corrija: bloqueado, y sin tocar una sola fila.
    with pytest.raises(RuntimeError, match="0179"):
        q.corregir_doble_retenciones(usuario="t", dry_run=False)
    assert not updates


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
