from __future__ import annotations
import sys
_REPO_ROOT = "/tmp/pc0706"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from tests.test_repro_bug_cobranza import Store, _login
import re

CAPT = []

def _wire(monkeypatch, fake_db, facturas, stub_movdoble=True):
    import db, mov_doble, error_messages
    from modules.cheques import queries as cq
    store = Store(facturas, fake_db)
    for name in ("fetch_one","fetch_all","execute","execute_returning","tx"):
        monkeypatch.setattr(db, name, getattr(store, name))
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    if stub_movdoble:
        monkeypatch.setattr(mov_doble, "registrar", lambda **k: None)
    _orig = error_messages.humanize
    def _spy(exc):
        CAPT.append(exc); return _orig(exc)
    monkeypatch.setattr(error_messages, "humanize", _spy)
    return store

def _mk(saldos):
    return {i+1: {"id_factura": i+1, "numf": 170000+i, "importe": float(s),
                  "abono": 0.0, "saldo": float(s), "stat": "Z"}
            for i, s in enumerate(saldos)}

def _post(client, aplicar, extra=None):
    data = {"paso":"ejecutar","codigo_cli":"MSS",
            "no_cheque[]":["","",""],"importe[]":["1353.69","881.73","2726.73"],
            "fechad[]":["","",""],"stat[]":["B","B","B"],
            "doc_banco[]":["36969717","35993449",""],"no_banco[]":["90","90","90"]}
    for idf, m in aplicar.items():
        data[f"aplicar[{idf}]"] = m
    if extra: data.update(extra)
    return client.post("/cheques/nuevo", data=data, follow_redirects=False)

def _err(resp):
    html = resp.get_data(as_text=True)
    m = re.search(r"estos datos[^<]*</p>\s*<ul[^>]*>(.*?)</ul>", html, re.S)
    return " | ".join(re.sub(r"\s+"," ",t).strip() for t in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S)) if m else ""

def test_aprobar_dif_con_2_centavos(client, fake_db, monkeypatch):
    CAPT.clear()
    saldos = ["1353.69","881.73","1000.00","900.00","500.00","326.75"]  # +0.02
    _wire(monkeypatch, fake_db, _mk(saldos))
    _login(client, fake_db)
    r = _post(client, {i+1: s for i, s in enumerate(saldos)},
              extra={"aprobar_diferencia":"1","motivo_diferencia":"redondeo"})
    print("APROBAR_DIF status:", r.status_code, "err:", _err(r)[:200], "caps:", [f"{type(e).__name__}: {e}" for e in CAPT])

def test_t_used_con_2_centavos(client, fake_db, monkeypatch):
    CAPT.clear()
    saldos = ["1353.69","881.73","1000.00","900.00","500.00","326.75"]
    _wire(monkeypatch, fake_db, _mk(saldos))
    _login(client, fake_db)
    r = _post(client, {i+1: s for i, s in enumerate(saldos)},
              extra={"aplicar_t_used":"1"})
    print("T_USED status:", r.status_code, "err:", _err(r)[:200], "caps:", [f"{type(e).__name__}: {e}" for e in CAPT])

def test_exacto_con_movdoble_real(client, fake_db, monkeypatch):
    CAPT.clear()
    saldos = ["1353.69","881.73","1000.00","900.00","500.00","326.73"]
    store = _wire(monkeypatch, fake_db, _mk(saldos), stub_movdoble=False)
    _login(client, fake_db)
    r = _post(client, {i+1: s for i, s in enumerate(saldos)})
    print("MOVDOBLE_REAL status:", r.status_code, "err:", _err(r)[:200],
          "caps:", [f"{type(e).__name__}: {e}" for e in CAPT], "unmatched:", store.unmatched[:4])
