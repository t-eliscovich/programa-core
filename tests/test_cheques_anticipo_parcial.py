"""Anticipo (97) aplicado PARCIALMENTE a facturas → espejo NB=98 por el resto.

TMT 2026-07-07 (dueña, caso CLR): "cuando pongo un anticipo en cobranza no
me dejaba deseleccionar cheques. si deselecciono, solo se tiene que ir a
nota de crédito y ya."

Antes el espejo de anticipo se creaba SOLO si NO había NINGUNA aplicación
(`_ch_es_anticipo = ... and not aplicaciones_pre`) — con aplicación parcial
el resto del anticipo moría en el cheque, en cartera. Ahora:

  - anticipo puro (sin aplicaciones)      → espejo por el TOTAL (igual que antes);
  - anticipo aplicado PARCIALMENTE        → aplicado a facturas + espejo por el RESTO;
  - anticipo aplicado ENTERO (resto < $1) → cobro normal, sin espejo.

Cubre también el helper nuevo queries.crear_espejo_anticipo (extraído de
crear() para reusar desde el view).
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ─────────────────────────── helper crear_espejo_anticipo ─────────────────

class _DBStubEspejo:
    def __init__(self):
        self.execute_returning_calls: list[tuple] = []
        self._next_id = 500

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.cheque" in s:
            self._next_id += 1
            return {"id_cheque": self._next_id}
        return {}


def test_helper_inserta_nb98_negativo_y_mov_doble(monkeypatch):
    import db
    import mov_doble
    from modules.cheques import queries as q

    s = _DBStubEspejo()
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    registrados: list[dict] = []
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: registrados.append(kw))

    r = q.crear_espejo_anticipo(
        conn=object(),
        id_cheque_padre=777,
        no_cheque="123",
        fecha=date(2026, 7, 7),
        fechad=date(2026, 7, 7),
        fecha_recibido=date(2026, 7, 7),
        codigo_cli="clr",
        importe_espejo=3000.0,
        usuario="test",
    )
    assert r.get("id_cheque") == 501
    ins = [
        (sql, params) for sql, params in s.execute_returning_calls
        if "insert into scintela.cheque" in " ".join(sql.split()).lower()
    ]
    assert len(ins) == 1
    _, params = ins[0]
    assert -3000.0 in params          # importe NEGATIVO
    assert 98 in params               # NB=98
    assert "ANTICIPO" in params       # banco texto (lista lo muestra así)
    assert "CLR" in params            # codigo_cli upper
    # fechad del espejo = fechad + 30 (paridad ALTAS.PRG L156)
    assert date(2026, 8, 6) in params
    # mov_doble tipo cheque_anticipo_espejo padre→espejo (historial intacto)
    assert len(registrados) == 1
    md = registrados[0]
    assert md["tipo"] == "cheque_anticipo_espejo"
    assert md["origen_id"] == 777
    assert md["destino_id"] == 501
    assert md["importe"] == -3000.0


def test_helper_sin_padre_no_registra_mov_doble(monkeypatch):
    import db
    import mov_doble
    from modules.cheques import queries as q

    s = _DBStubEspejo()
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    registrados: list[dict] = []
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: registrados.append(kw))
    q.crear_espejo_anticipo(
        conn=object(), id_cheque_padre=None, fecha=date(2026, 7, 7),
        codigo_cli="CLR", importe_espejo=100.0,
    )
    assert registrados == []


# ─────────────────────────── view: POST /cheques/nuevo con 97 ─────────────

class Store:
    """Router de SQL en memoria (mismo patrón que test_repro_bug_cobranza)."""

    def __init__(self, facturas, fake_seg):
        self.facturas = facturas
        self.fake_seg = fake_seg
        self.cheques = {}
        self.next_cheque = 90001
        self.cxf = []
        self.unmatched = []

    def _norm(self, sql):
        return " ".join(sql.split()).lower()

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
        self.unmatched.append(("fetch_one", s, params))
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = self._norm(sql)
        if "seguridad." in s:
            return self.fake_seg.fetch_all(sql, params, conn)
        if "select id_factura, saldo from scintela.factura where id_factura = any" in s:
            return [
                {"id_factura": i, "saldo": self.facturas[i]["saldo"]}
                for i in params[0] if i in self.facturas
            ]
        if "select id_factura, numf, numf_completo from scintela.factura" in s:
            return [
                {"id_factura": i, "numf": self.facturas[i]["numf"],
                 "numf_completo": None}
                for i in params[0] if i in self.facturas
            ]
        if "from scintela.factura where codigo_cli" in s:
            return []
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
                "codigo_cli": "CLR",
                "importe": float(params[5]),
                "no_banco": params[6],
                "stat": "Z",
                "fecha": params[1],
            }
            self.cheques[cid] = row
            return {"id_cheque": cid, "no_cheque": params[0]}
        return None

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _wire(monkeypatch, fake_db, facturas):
    import db
    import mov_doble
    from modules.cheques import queries as cq

    store = Store(facturas, fake_db)
    for name in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, name, getattr(store, name))
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(mov_doble, "registrar", lambda **k: None)
    return store


def _login(client, fake_db):
    rid = fake_db.add_role("Admin", ["*", "cheques.crear", "cheques.ver"])
    uid = fake_db.add_user("tam", b"x", rid)
    with client.session_transaction() as sess:
        sess["user_id"] = uid


def _post_97(client, importe, aplicar=None):
    data = {
        "paso": "ejecutar",
        "codigo_cli": "CLR",
        "no_cheque[]": ["123"],
        "importe[]": [importe],
        "fechad[]": [""],
        "stat[]": ["Z"],
        "doc_banco[]": [""],
        "no_banco[]": ["97"],   # ANTICIPO
    }
    for idf, monto in (aplicar or {}).items():
        data[f"aplicar[{idf}]"] = monto
    return client.post("/cheques/nuevo", data=data, follow_redirects=False)


def _factura(idf, saldo):
    return {idf: {"id_factura": idf, "numf": 170000 + idf, "importe": float(saldo),
                  "abono": 0.0, "saldo": float(saldo), "stat": "Z"}}


def _espejos(store):
    return [c for c in store.cheques.values()
            if c["no_banco"] == 98 and c["importe"] < 0]


def test_anticipo_parcial_crea_espejo_por_el_resto(client, fake_db, monkeypatch):
    # Anticipo $5.000, aplica $2.000 a la factura → espejo NB=98 de −3.000.
    store = _wire(monkeypatch, fake_db, _factura(1, "2000.00"))
    _login(client, fake_db)
    resp = _post_97(client, "5000.00", aplicar={1: "2000.00"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    esp = _espejos(store)
    assert len(esp) == 1
    assert esp[0]["importe"] == pytest.approx(-3000.0)
    # la factura quedó cobrada y hay 1 aplicación
    assert store.facturas[1]["abono"] == pytest.approx(2000.0)
    assert len(store.cxf) == 1
    # el principal quedó por el total del anticipo (97 sin medio)
    principal = [c for c in store.cheques.values() if c["importe"] > 0]
    assert len(principal) == 1
    assert principal[0]["importe"] == pytest.approx(5000.0)


def test_anticipo_aplicado_entero_sin_espejo(client, fake_db, monkeypatch):
    # Anticipo $2.000 aplicado entero → cobro normal, SIN espejo.
    store = _wire(monkeypatch, fake_db, _factura(1, "2000.00"))
    _login(client, fake_db)
    resp = _post_97(client, "2000.00", aplicar={1: "2000.00"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    assert _espejos(store) == []
    assert store.facturas[1]["abono"] == pytest.approx(2000.0)


def test_anticipo_resto_menor_a_un_dolar_sin_espejo(client, fake_db, monkeypatch):
    # Resto de $0,50 = centavos → no genera NC (mismo umbral de siempre).
    store = _wire(monkeypatch, fake_db, _factura(1, "2000.00"))
    _login(client, fake_db)
    resp = _post_97(client, "2000.50", aplicar={1: "2000.00"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    assert _espejos(store) == []


def test_anticipo_puro_espejo_total_intacto(client, fake_db, monkeypatch):
    # Sin aplicaciones → flujo clásico: espejo por el TOTAL (no cambió).
    store = _wire(monkeypatch, fake_db, _factura(1, "2000.00"))
    _login(client, fake_db)
    resp = _post_97(client, "5000.00")
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    esp = _espejos(store)
    assert len(esp) == 1
    assert esp[0]["importe"] == pytest.approx(-5000.0)
    # ninguna factura tocada
    assert store.facturas[1]["abono"] == 0.0
    assert store.cxf == []


def test_flash_menciona_nota_de_credito(client, fake_db, monkeypatch):
    _wire(monkeypatch, fake_db, _factura(1, "2000.00"))
    _login(client, fake_db)
    resp = _post_97(client, "5000.00", aplicar={1: "2000.00"},)
    assert resp.status_code == 302
    # el flash viaja en la sesión hasta el próximo render
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes") or []
    textos = " | ".join(m for _, m in flashes)
    assert "3,000.00" in textos
    assert re.search(r"nota de cr", textos, re.I)
