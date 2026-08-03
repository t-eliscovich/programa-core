"""Deshacer un cambio de estado de factura desde el ↺ del Historial.

TMT 2026-08-03 (dueña: *"totalicé mal y no me deja volver"*). El ↺ contestaba
"Reverso no automatizado para este tipo. Volvé a cambiarle el estado desde el
estado de cuenta (el selector de la fila)" — y era cierto, pero al pasar a 'T'
la factura SALE del listado del estado de cuenta y queda detrás del botón "Ver
totalizadas": el consejo la mandaba a una fila que no estaba a la vista.

El snapshot (stat/abono/saldo previos) ya se guardaba en el mov, así que el
reverso es EXACTO. Estos tests clavan las dos mitades: que vuelve el estado
correcto, y que se BLOQUEA cuando deshacerlo pisaría algo posterior.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DBStub:
    """Devuelve el mov, la factura y el 'último cambio activo' por separado."""

    def __init__(self, mov, factura, ultimo_id=7):
        self.mov = mov
        self.factura = factura
        self.ultimo_id = ultimo_id
        self.updates: list[tuple] = []
        self.movs: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura) if self.factura else None
        if "order by id_mov_doble desc limit 1" in s:
            return {"id_mov_doble": self.ultimo_id} if self.ultimo_id else None
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        return None

    def execute(self, sql, params=None, conn=None):
        if "update scintela.factura" in " ".join(sql.split()).lower():
            self.updates.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.mov_doble" in " ".join(sql.split()).lower():
            self.movs.append(tuple(params))
            return {"id_mov_doble": 999}
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


def _mov(**kw):
    base = {
        "id_mov_doble": 7, "tipo": "factura_stat_cambio", "estado": "activo",
        "origen_id": 5,
        "metadata": {"id_factura": 5, "codigo_cli": "FET", "stat_previo": "A",
                     "stat_nuevo": "T", "abono_previo": 7.87,
                     "saldo_previo": 444.67},
    }
    base.update(kw)
    return base


def _fac(**kw):
    base = {"id_factura": 5, "numf": 175737, "numf_completo": "001-001-175737",
            "codigo_cli": "FET", "importe": 452.54, "abono": 7.87,
            "saldo": 0.0, "stat": "T"}
    base.update(kw)
    return base


# --- el caso de la dueña ----------------------------------------------------

def test_deshacer_devuelve_la_factura_a_su_estado_previo(monkeypatch):
    """A→T deshecho: vuelve a 'A' con el saldo y el abono de antes."""
    stub = _DBStub(_mov(), _fac())
    _patch(monkeypatch, stub)
    from modules.informes import queries

    res = queries.factura_deshacer_cambio_stat(7, usuario="tamara")

    assert res["stat_destino"] == "A"
    assert res["saldo_destino"] == 444.67
    assert len(stub.updates) == 1
    stat, saldo, abono, usuario, id_factura = stub.updates[0]
    assert (stat, saldo, abono, id_factura) == ("A", 444.67, 7.87, 5)
    assert usuario == "tamara"


def test_el_mov_original_queda_marcado_como_reversado(monkeypatch):
    """Sin esto el ↺ sigue ofreciéndose y se puede deshacer dos veces."""
    stub = _DBStub(_mov(), _fac())
    _patch(monkeypatch, stub)
    from modules.informes import queries

    queries.factura_deshacer_cambio_stat(7)

    assert stub.movs, "no registró el mov del reverso"
    # `registrar(id_original=...)` marca el original; el id va en la fila nueva.
    assert 7 in stub.movs[0], (
        "el mov del reverso no apunta al original (id_original) — el ↺ del "
        "historial va a seguir apareciendo sobre un cambio ya deshecho"
    )


# --- guardas ----------------------------------------------------------------

def test_bloquea_si_hubo_cobranza_despues(monkeypatch):
    """El abono de hoy ≠ el de entonces ⇒ restaurar el saldo pisaría plata."""
    stub = _DBStub(_mov(), _fac(abono=300.0))
    _patch(monkeypatch, stub)
    from modules.informes import queries

    prev = queries.factura_cambio_stat_preview(7)
    assert prev["bloqueo"] and "cobranza" in prev["bloqueo"].lower()
    with pytest.raises(ValueError):
        queries.factura_deshacer_cambio_stat(7)
    assert not stub.updates, "bloqueado y aun así escribió la factura"


def test_bloquea_si_hubo_otro_cambio_posterior(monkeypatch):
    """Deshacer uno del medio deja los snapshots encadenados apuntando al aire."""
    stub = _DBStub(_mov(), _fac(), ultimo_id=12)
    _patch(monkeypatch, stub)
    from modules.informes import queries

    prev = queries.factura_cambio_stat_preview(7)
    assert prev["bloqueo"] and "#12" in prev["bloqueo"]
    with pytest.raises(ValueError):
        queries.factura_deshacer_cambio_stat(7)
    assert not stub.updates


def test_bloquea_si_ya_estaba_reversado(monkeypatch):
    stub = _DBStub(_mov(estado="reversado"), _fac())
    _patch(monkeypatch, stub)
    from modules.informes import queries

    prev = queries.factura_cambio_stat_preview(7)
    assert prev["bloqueo"] and "reversado" in prev["bloqueo"]
    with pytest.raises(ValueError):
        queries.factura_deshacer_cambio_stat(7)
    assert not stub.updates


def test_otro_tipo_de_mov_no_se_toca(monkeypatch):
    stub = _DBStub(_mov(tipo="cheque_aplicado"), _fac())
    _patch(monkeypatch, stub)
    from modules.informes import queries

    assert queries.factura_cambio_stat_preview(7) == {}
    with pytest.raises(ValueError):
        queries.factura_deshacer_cambio_stat(7)


# --- el cableado con el Historial -------------------------------------------

def test_los_dos_tipos_salieron_de_la_lista_de_bloqueados():
    from modules.historial.views import _REVERSO_BLOQUEADO, _REVERSO_DISPATCH

    for tipo in ("factura_stat_cambio", "factura_cerrada_a_t"):
        assert tipo in _REVERSO_DISPATCH, f"{tipo} sin handler de reverso"
        assert tipo not in _REVERSO_BLOQUEADO, (
            f"{tipo} está en las dos listas — el bloqueo gana y el ↺ vuelve a "
            "mandar a la dueña a buscar la fila a mano"
        )


def test_todo_endpoint_del_dispatcher_existe_de_verdad(app):
    """Regla general: un handler de reverso que apunta a un endpoint que no
    existe revienta con BuildError recién cuando alguien clickea el ↺."""
    from modules.historial.views import _REVERSO_DISPATCH

    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    faltan = sorted(
        ep for ep, _ in _REVERSO_DISPATCH.values() if ep not in endpoints)
    assert not faltan, f"handlers apuntando a endpoints inexistentes: {faltan}"
