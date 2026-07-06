"""Repro bug 2026-07-06 — cobranza MSS 3 cheques banco 90 + rango desdeHhasta.

Reproduce el POST paso=ejecutar real contra un stub de DB en memoria y
captura la excepción REAL detrás de "Hubo un problema. Avisá a soporte".
"""
from __future__ import annotations

import contextlib
import os
import re
import sys

import pytest

_REPO_ROOT = "/tmp/pc0706"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class Store:
    """Estado mutable de facturas/cheques + router de SQL."""

    def __init__(self, facturas, fake_seg):
        # facturas: dict id -> {id_factura, numf, importe, abono, saldo, stat}
        self.facturas = facturas
        self.fake_seg = fake_seg           # FakeDB de conftest (seguridad.*)
        self.cheques = {}
        self.next_cheque = 90001
        self.next_trans = 70001
        self.cxf = []                      # chequesxfact insertados
        self.unmatched = []                # SQL no ruteado (diagnóstico)

    # ---------- helpers ----------
    def _norm(self, sql):
        return " ".join(sql.split()).lower()

    # ---------- API db.* ----------
    def fetch_one(self, sql, params=None, conn=None):
        s = self._norm(sql)
        if "seguridad." in s:
            return self.fake_seg.fetch_one(sql, params, conn)
        if "select 1 as x from scintela.cliente" in s:
            return {"x": 1}
        if "from scintela.factura where id_factura = %s" in s:
            f = self.facturas.get(int(params[0]))
            return dict(f) if f else None
        if "from scintela.cheque where id_cheque = %s" in s:
            c = self.cheques.get(int(params[0]))
            return dict(c) if c else None
        if "from scintela.banco" in s:
            return {"no_banco": 10}
        if "from scintela.transacciones_bancarias" in s:
            # _saldo_previo / later_row: sin filas previas ni posteriores.
            return None
        self.unmatched.append(("fetch_one", s, params))
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = self._norm(sql)
        if "seguridad." in s:
            return self.fake_seg.fetch_all(sql, params, conn)
        if "select id_factura, saldo from scintela.factura where id_factura = any" in s:
            ids = params[0]
            return [
                {"id_factura": i, "saldo": self.facturas[i]["saldo"]}
                for i in ids if i in self.facturas
            ]
        if "select id_factura, numf, numf_completo from scintela.factura" in s:
            ids = params[0]
            return [
                {"id_factura": i, "numf": self.facturas[i]["numf"],
                 "numf_completo": None}
                for i in ids if i in self.facturas
            ]
        if "from scintela.factura where codigo_cli" in s:
            return []  # NCs libres
        self.unmatched.append(("fetch_all", s, params))
        return []

    def execute(self, sql, params=None, conn=None):
        s = self._norm(sql)
        if "update scintela.factura set abono" in s:
            abono, saldo, stat, _usr, idf = params
            f = self.facturas[int(idf)]
            f["abono"], f["saldo"], f["stat"] = float(abono), float(saldo), stat
            return 1
        if "insert into scintela.chequesxfact" in s:
            self.cxf.append(params)
            return 1
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = self._norm(sql)
        if "insert into scintela.cheque" in s:
            cid = self.next_cheque
            self.next_cheque += 1
            row = {
                "id_cheque": cid,
                "no_cheque": params[0],
                "codigo_cli": "MSS",
                "importe": float(params[5]),
                "no_banco": params[6],
                "stat": params[8] if len(params) > 8 else "Z",
                "fecha": params[1],
            }
            self.cheques[cid] = row
            return {"id_cheque": cid, "no_cheque": params[0]}
        if "insert into scintela.transacciones_bancarias" in s:
            tid = self.next_trans
            self.next_trans += 1
            return {"id_transaccion": tid}
        if "insert into scintela.caja" in s:
            return {"id_caja": 555}
        self.unmatched.append(("execute_returning", s, params))
        return None

    @contextlib.contextmanager
    def tx(self):
        yield object()


CAPTURADAS = []


def _wire(monkeypatch, fake_db, facturas):
    import db
    import mov_doble
    import error_messages
    from modules.cheques import queries as cq

    store = Store(facturas, fake_db)
    for name in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, name, getattr(store, name))
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(mov_doble, "registrar", lambda **k: None)

    _orig = error_messages.humanize

    def _spy(exc):
        CAPTURADAS.append(exc)
        return _orig(exc)

    monkeypatch.setattr(error_messages, "humanize", _spy)
    return store


def _login(client, fake_db):
    rid = fake_db.add_role("Admin", ["*", "cheques.crear", "cheques.ver"])
    uid = fake_db.add_user("tam", b"x", rid)
    with client.session_transaction() as sess:
        sess["user_id"] = uid


def _post(client, aplicar, importes=("1353.69", "881.73", "2726.73"),
          docs=("36969717", "35993449", ""), extra=None):
    data = {
        "paso": "ejecutar",
        "codigo_cli": "MSS",
        "no_cheque[]": ["", "", ""][: len(importes)],
        "importe[]": list(importes),
        "fechad[]": ["" for _ in importes],
        "stat[]": ["B" for _ in importes],
        "doc_banco[]": list(docs[: len(importes)]),
        "no_banco[]": ["90" for _ in importes],
    }
    for idf, monto in aplicar.items():
        data[f"aplicar[{idf}]"] = monto
    if extra:
        data.update(extra)
    return client.post("/cheques/nuevo", data=data, follow_redirects=False)


def _facturas(saldos):
    out = {}
    for i, s in enumerate(saldos, start=1):
        out[i] = {"id_factura": i, "numf": 170000 + i, "importe": float(s),
                  "abono": 0.0, "saldo": float(s), "stat": "Z"}
    return out


def _errores_html(resp):
    html = resp.get_data(as_text=True)
    return re.findall(r"<li[^>]*>(.*?)</li>", html, re.S)


# ─── Caso A: suma de saldos EXACTA = 4962.15 (match perfecto) ──────────────
def test_caso_exacto(client, fake_db, monkeypatch):
    CAPTURADAS.clear()
    saldos = ["1353.69", "881.73", "1000.00", "900.00", "500.00", "326.73"]
    assert abs(sum(map(float, saldos)) - 4962.15) < 1e-9
    store = _wire(monkeypatch, fake_db, _facturas(saldos))
    _login(client, fake_db)
    aplicar = {i + 1: s for i, s in enumerate(saldos)}
    resp = _post(client, aplicar)
    print("STATUS:", resp.status_code)
    if resp.status_code != 302:
        print("ERRORES:", _errores_html(resp))
    print("CAPTURADAS:", [repr(e) for e in CAPTURADAS])
    print("UNMATCHED:", store.unmatched)
    assert resp.status_code == 302, "el caso exacto deberia guardar"


# ─── Caso B: sobre-aplicación de 1 centavo (saldos suman 4962.16) ──────────
def test_caso_un_centavo_de_mas(client, fake_db, monkeypatch):
    CAPTURADAS.clear()
    saldos = ["1353.69", "881.73", "1000.00", "900.00", "500.00", "326.74"]
    assert abs(sum(map(float, saldos)) - 4962.16) < 1e-9
    store = _wire(monkeypatch, fake_db, _facturas(saldos))
    _login(client, fake_db)
    aplicar = {i + 1: s for i, s in enumerate(saldos)}
    resp = _post(client, aplicar)
    print("STATUS:", resp.status_code)
    if resp.status_code != 302:
        print("ERRORES:", _errores_html(resp))
    print("CAPTURADAS:", [repr(e) for e in CAPTURADAS])
    print("UNMATCHED:", store.unmatched)


# ─── Caso C: 4 centavos de más ──────────────────────────────────────────────
def test_caso_cuatro_centavos_de_mas(client, fake_db, monkeypatch):
    CAPTURADAS.clear()
    saldos = ["1353.69", "881.73", "1000.00", "900.00", "500.00", "326.77"]
    store = _wire(monkeypatch, fake_db, _facturas(saldos))
    _login(client, fake_db)
    aplicar = {i + 1: s for i, s in enumerate(saldos)}
    resp = _post(client, aplicar)
    print("STATUS:", resp.status_code)
    if resp.status_code != 302:
        print("ERRORES:", _errores_html(resp))
    print("CAPTURADAS:", [repr(e) for e in CAPTURADAS])
    print("UNMATCHED:", store.unmatched)


# ─── Caso D: última factura PARTIDA entre 2 cheques + centavos ─────────────
def test_caso_factura_partida_mas_centavo(client, fake_db, monkeypatch):
    CAPTURADAS.clear()
    # factura 3 grande: la cubre parte del cheque 1 + cheque 2 + cheque 3.
    saldos = ["500.00", "300.00", "4162.16"]  # suma 4962.16 (+1 centavo)
    store = _wire(monkeypatch, fake_db, _facturas(saldos))
    _login(client, fake_db)
    aplicar = {i + 1: s for i, s in enumerate(saldos)}
    resp = _post(client, aplicar)
    print("STATUS:", resp.status_code)
    if resp.status_code != 302:
        print("ERRORES:", _errores_html(resp))
    print("CAPTURADAS:", [repr(e) for e in CAPTURADAS])
    print("UNMATCHED:", store.unmatched)
