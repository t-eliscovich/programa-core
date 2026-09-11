"""deshacer_neteo re-aplica TAMBIÉN las aplicaciones negativas (caso CJM, 11/09/2026).

El cheque #01 de CJM ($3.411) pagaba 4 facturas y absorbía 3 devoluciones
(aplicaciones con importe < 0). El neteo lo desaplicó de las 7 y guardó las 7
en el snapshot; al deshacer, el paso "Re-aplicar" salteaba `imp <= 0` y sólo
volvía a aplicar las 4 positivas: cheque sobre-aplicado por 5.372,12 y las
devoluciones abiertas. El docstring prometía "el estado PREVIO exacto".

Ahora se saltea sólo el cero: cada aplicación del snapshot (positiva o
negativa) vuelve a escribir la factura y su fila de chequesxfact.
"""
from __future__ import annotations

import contextlib
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_EVENTO = 24309
_CLI = "CJM"


class _DBStub:
    """Snapshot con una positiva, una negativa y un cero; facturas sin abono."""

    def __init__(self):
        self.facturas = {
            278961: {"importe": 3411.01, "abono": 0, "retencion": 0},
            279341: {"importe": -391.43, "abono": 0, "retencion": 0},
            279999: {"importe": 10.0, "abono": 0, "retencion": 0},
        }
        self.updates_factura: list[tuple] = []
        self.inserts_cxf: list[tuple] = []
        self.evento_estado = "activo"

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.mov_doble" in s:
            return {
                "id_mov_doble": _EVENTO, "tipo": "neteo_estado_cuenta",
                "estado": self.evento_estado, "importe": 3411, "batch_id": "b1",
                "metadata": {
                    "codigo_cli": _CLI, "id_residuo": None,
                    "ids_anticipos": [102344], "ids_cheques": [101564],
                    "anticipos": [{"id": 102344, "stat_prev": "Z"}],
                    "cheques": [{
                        "id": 101564, "stat_prev": "P", "posdat": [],
                        "aplicaciones": [
                            {"id_factura": 278961, "importe": 3411.01, "numf": "179989"},
                            {"id_factura": 279341, "importe": -391.43, "numf": "11412"},
                            {"id_factura": 279999, "importe": 0.0, "numf": "0"},
                        ],
                    }],
                },
            }
        if "select stat from scintela.cheque" in s:
            return {"stat": "X"}
        if "select no_banco from scintela.cheque" in s:
            return {"no_banco": 30}
        if "from scintela.chequesxfact" in s:
            return None  # todavía no re-aplicada
        if "from scintela.factura" in s:
            return dict(self.facturas[params[0]])
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates_factura.append(tuple(params))
        elif "insert into scintela.chequesxfact" in s:
            self.inserts_cxf.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return {"id_mov_doble": 1}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _run(monkeypatch):
    import db
    import mov_doble
    from modules.cheques import queries as chq

    stub = _DBStub()
    for name in ("fetch_all", "fetch_one", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, name, getattr(stub, name))
    monkeypatch.setattr(chq, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: 1)
    chq.deshacer_neteo(_EVENTO, _CLI, usuario="test")
    return stub


def test_reaplica_la_negativa_y_la_positiva(monkeypatch):
    stub = _run(monkeypatch)
    # UPDATE factura: (abono, saldo, stat, usuario, id_factura)
    por_factura = {p[4]: p for p in stub.updates_factura}
    assert 278961 in por_factura, "la positiva se re-aplica (como siempre)"
    assert 279341 in por_factura, "la DEVOLUCIÓN (negativa) también se re-aplica"
    abono, saldo, stat = por_factura[279341][:3]
    assert abono == -391.43 and saldo == 0 and stat == "T"


def test_la_aplicacion_en_cero_se_saltea(monkeypatch):
    stub = _run(monkeypatch)
    ids = {p[4] for p in stub.updates_factura}
    assert 279999 not in ids
    assert len(stub.inserts_cxf) == 2


def test_el_filtro_no_es_por_signo(monkeypatch):
    """Mutante: si vuelve `imp <= 0`, este test se pone rojo."""
    import inspect

    from modules.cheques import queries as chq

    src = inspect.getsource(chq.deshacer_neteo)
    assert "if imp <= 0" not in src
    assert "abs(imp) < 0.005" in src
