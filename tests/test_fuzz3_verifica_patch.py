from __future__ import annotations
import sys
_REPO_ROOT = "/tmp/pc0706"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from tests.test_fuzz2_bug_cobranza import _wire, _mk, _post, CAPT
from tests.test_repro_bug_cobranza import _login

def test_dos_centavos_semantica(client, fake_db, monkeypatch):
    CAPT.clear()
    saldos = ["1353.69","881.73","1000.00","900.00","500.00","326.75"]  # +0.02
    store = _wire(monkeypatch, fake_db, _mk(saldos))
    _login(client, fake_db)
    r = _post(client, {i+1: s for i, s in enumerate(saldos)})
    assert r.status_code == 302
    f6 = store.facturas[6]
    print("f6:", f6)
    assert abs(f6["abono"] - 326.73) < 0.001
    assert abs(f6["saldo"] - 0.02) < 0.001
    assert f6["stat"] == "T"
    # el resto de las facturas cerradas exactas
    for i in range(1, 6):
        assert store.facturas[i]["stat"] == "T", store.facturas[i]
        assert abs(store.facturas[i]["saldo"]) < 0.001
    # total aplicado por cheque == importe del cheque
    tot = sum(float(p[3]) for p in store.cxf)  # params[3] = importe
    print("total cxf:", tot)
    assert abs(tot - 4962.15) < 0.001

def test_stat_final_A_respetado(client, fake_db, monkeypatch):
    CAPT.clear()
    saldos = ["1353.69","881.73","1000.00","900.00","500.00","326.75"]
    store = _wire(monkeypatch, fake_db, _mk(saldos))
    _login(client, fake_db)
    r = _post(client, {i+1: s for i, s in enumerate(saldos)},
              extra={"stat_final[6]": "A"})
    assert r.status_code == 302
    print("f6:", store.facturas[6])
    assert store.facturas[6]["stat"] == "A"
