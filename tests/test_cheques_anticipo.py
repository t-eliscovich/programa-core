"""Tests para el flag `es_anticipo` en cheques.crear (CONCEPTO=9999 legacy).

Cuando `es_anticipo=True`:
1. Se inserta el cheque normal.
2. Se inserta un "espejo" con importe negativo, stat='Z', id_cheque_padre=cheque_principal.

Cuando `es_anticipo=False` (default):
3. Solo se inserta el cheque normal — sin espejo.

Cuando `es_anticipo=True` pero `importe<=0`:
4. NO se inserta espejo (no tiene sentido).
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


class _DBStub:
    def __init__(self):
        self.execute_returning_calls: list[tuple] = []
        self._next_id = 100

    def fetch_one(self, sql, params=None, conn=None):
        return None

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.cheque" in s:
            self._next_id += 1
            return {"id_cheque": self._next_id, "no_cheque": "100"}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import modules.cheques.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


def test_cheque_normal_no_crea_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        es_anticipo=False,  # explícito
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1


def test_cheque_anticipo_crea_espejo_negativo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        es_anticipo=True,
    )
    assert r.get("id_cheque_anticipo") is not None
    # Dos INSERTs: cheque principal + espejo
    assert len(stub.execute_returning_calls) == 2

    # El segundo es el espejo, importe debe ser negativo (-500)
    sql_espejo, params_espejo = stub.execute_returning_calls[1]
    assert "insert into scintela.cheque" in sql_espejo.lower()
    assert -500.0 in params_espejo


def test_cheque_anticipo_pero_importe_cero_no_crea_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=0, no_banco=1, banco_texto="Pichincha",
        es_anticipo=True,
    )
    # Solo el cheque principal (con importe=0), no espejo
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1


def test_cheque_anticipo_default_es_false(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        # no paso es_anticipo
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1


# ── El 95 acepta anticipos POSTERGADOS (TMT 2026-07-30) ────────────────────
# Dueña: "deberia mostrar postergados igual, no se porque filtramos". El primer
# caso real falló por esto: el espejo de GL1 (−900) estaba postergado, y
# postergar sólo mueve la fecha de depósito — el crédito del cliente sigue ahí.
# Si esto se vuelve a acotar a 'Z', el 95 deja el cheque en cartera con un aviso
# y la dueña se entera después.

def test_match_del_95_acepta_el_grupo_vivo_no_solo_Z():
    import inspect

    from modules.cheques import queries as q
    src = inspect.getsource(q.crear)
    i = src.find("no_banco == 95")
    assert i > 0, "cambió el bloque del 95 — revisar este test"
    bloque = src[i:i + 1200]
    assert "IN ('Z','1','2','3','P','D')" in bloque, (
        "el match del 95 volvió a exigir stat='Z': los anticipos postergados "
        "dejan de encontrarse"
    )
    assert "= 'Z') DESC" in bloque, "los de cartera tienen que ir primero"


def test_anticipos_vivos_lista_los_postergados():
    import inspect

    from modules.cheques import queries as q
    src = inspect.getsource(q.anticipos_vivos)
    assert "IN ('Z', '1', '2', '3', 'P', 'D')" in src
    assert "no_banco IN (97, 98)" in src
    assert "COALESCE(importe, 0) < 0" in src, "el espejo del anticipo es negativo"


# ── El 95 tiene que poder CERRAR la factura que el anticipo paga ───────────
# TMT 2026-07-30. `crear()` anula el 95 contra el espejo NB=98 (los dos a 'X')
# en la misma tx, y después el flujo de cobranza lo aplica a la factura. El
# guard de stats aplicables (#26) rechazaba ese 'X' y la cobranza entera moría
# con "Cheque N en stat='X' no se puede aplicar a facturas" — el saldo a favor
# del cliente no había forma de usarlo. La excepción es angosta a propósito:
# sólo el flujo de creación (permitir_depositado) y sólo no_banco=95.

class _StubAplicar:
    def __init__(self, stat: str, no_banco: int):
        self.stat, self.no_banco = stat, no_banco
        self.updates: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque" in s:
            return {"id_cheque": 501, "codigo_cli": "GL1", "no_banco": self.no_banco,
                    "importe": 900.0, "stat": self.stat, "fecha": date(2026, 7, 30)}
        if "from scintela.factura" in s:
            return {"id_factura": 9001, "numf": 178474, "importe": 1874.91,
                    "abono": 974.91, "saldo": 900.0, "stat": "A"}
        return None

    def execute(self, sql, params=None, conn=None):
        self.updates.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1


@pytest.fixture
def _aplicar(monkeypatch):
    def _run(stat, no_banco, permitir_depositado=True):
        import db
        import mov_doble
        from modules.cheques import queries as q
        s = _StubAplicar(stat, no_banco)
        monkeypatch.setattr(db, "fetch_one", s.fetch_one)
        monkeypatch.setattr(db, "execute", s.execute)
        monkeypatch.setattr(mov_doble, "registrar", lambda **kw: None)
        r = q.aplicar_a_factura(
            id_cheque=501,
            aplicaciones=[{"id_fact": 9001, "importe": 900.0}],
            conn=object(),
            permitir_depositado=permitir_depositado,
        )
        return s, r
    return _run


def test_el_95_anulado_igual_cierra_la_factura(_aplicar):
    s, r = _aplicar("X", 95)
    assert r["total_aplicado"] == 900.0
    upd = [u for u in s.updates if "update scintela.factura" in u[0]]
    assert upd, "no actualizó la factura"
    abono, saldo, stat = upd[0][1][0], upd[0][1][1], upd[0][1][2]
    assert round(abono, 2) == 1874.91
    assert abs(saldo) < 0.005
    assert stat == "T", "la factura tiene que quedar cancelada"


def test_un_cheque_anulado_que_no_es_95_sigue_bloqueado(_aplicar):
    with pytest.raises(ValueError, match="stat='X'"):
        _aplicar("X", 90)


def test_la_ruta_manual_no_puede_aplicar_un_95_anulado(_aplicar):
    # /cheques/<id>/aplicar pasa permitir_depositado=False: un 95 ya anulado
    # (de otra cobranza, otro día) no se re-aplica desde ahí.
    with pytest.raises(ValueError, match="stat='X'"):
        _aplicar("X", 95, permitir_depositado=False)
