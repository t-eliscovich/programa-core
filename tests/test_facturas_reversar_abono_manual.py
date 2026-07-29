"""Deshacer un abono cargado a mano — y NO deshacerlo cuando pisaría algo.

TMT 2026-07-29. El mov_doble 'factura_abono_manual' se creó el 14/05 con el
comentario "esto permite verlo en /historial y, eventualmente, reversarlo".
El "eventualmente" nunca llegó: 14 movs activos visibles y no deshacibles, en
el campo donde un cero de más manda la factura a stat='T' y la saca de la
cobranza.

Lo que estos tests fijan no es tanto el camino feliz (restaurar un número es
fácil) sino las dos precondiciones, que es donde un reverso de plata se
vuelve peligroso:

  - si el abono ya NO es el que dejó esta edición, alguien aplicó un cheque o
    editó de nuevo en el medio → reversar pisaría ese cambio posterior;
  - si el importe cambió (toggle de pronto pago 5%), el saldo se recomputa con
    el importe de HOY, nunca con el viejo — si no, quedaría
    `saldo ≠ importe − abono`, que es justo uno de los errores que el chequeo
    de salud persigue.
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _FakeDB:
    """mov_doble + factura, lo mínimo para `reversar_abono_manual`."""

    def __init__(self, mov: dict | None, fact: dict | None):
        self.mov = dict(mov) if mov else None
        self.fact = dict(fact) if fact else None
        self.updates: list[tuple[str, tuple]] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        if "from scintela.factura" in s:
            return dict(self.fact) if self.fact else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.updates.append((" ".join((sql or "").split()), tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return None

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)

        @contextlib.contextmanager
        def _fake_tx(*_a, **_k):
            yield object()

        monkeypatch.setattr(db_mod, "tx", _fake_tx)


def _mov(abono_prev=100.0, abono_nuevo=500.0, estado="activo",
         tipo="factura_abono_manual", meta=True):
    m = {
        "id_mov_doble": 9001,
        "tipo": tipo,
        "origen_id": 55,
        "estado": estado,
        "importe": round(abono_nuevo - abono_prev, 2),
        "fecha_operacion": date(2026, 7, 20),
        "metadata": ({"abono_prev": abono_prev, "abono_nuevo": abono_nuevo,
                      "id_factura": 55} if meta else {}),
    }
    return m


def _fact(importe=1000.0, abono=500.0, stat="A"):
    return {
        "id_factura": 55, "numf": 12345, "numf_completo": "001-12345",
        "fecha": date(2026, 7, 20), "importe": importe, "abono": abono,
        "saldo": round(importe - abono, 2), "stat": stat,
        "codigo_cli": "AIG",
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)
    import modules.facturas.queries as q
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None)
    import mov_doble
    registradas: list[dict] = []
    monkeypatch.setattr(mov_doble, "registrar",
                        lambda **kw: registradas.append(kw) or 1)
    return registradas


def _correr(monkeypatch, mov, fact):
    import db as db_mod
    import modules.facturas.queries as q
    fake = _FakeDB(mov, fact)
    fake.apply_to(monkeypatch, db_mod)
    return q, fake


def test_restaura_el_abono_anterior_y_recomputa_saldo_y_stat(monkeypatch, _stubs):
    """Camino feliz: 500 → 100, saldo 900, la factura vuelve de 'A' a 'A'."""
    q, fake = _correr(monkeypatch, _mov(100.0, 500.0), _fact(1000.0, 500.0, "A"))
    res = q.reversar_abono_manual(9001, usuario="tamara")
    assert res["abono_previo"] == 500.0
    assert res["abono_actual"] == 100.0
    assert res["saldo"] == 900.0
    assert res["stat"] == "A"
    upd = [u for u in fake.updates if "update scintela.factura" in u[0].lower()]
    assert len(upd) == 1, "tiene que escribir la factura una sola vez"
    assert upd[0][1][:3] == (100.0, 900.0, "A")


def test_el_reverso_saca_la_factura_de_cancelada(monkeypatch, _stubs):
    """El caso real: el cero de más la había puesto en 'T' (cancelada) y la
    sacó de la cobranza. Al deshacer tiene que volver a 'Z' y con saldo."""
    q, _ = _correr(monkeypatch, _mov(0.0, 1000.0), _fact(1000.0, 1000.0, "T"))
    res = q.reversar_abono_manual(9001)
    assert res["stat"] == "Z", "sin abono la factura vuelve a emitida"
    assert res["saldo"] == 1000.0


def test_no_reversa_si_el_abono_cambio_despues(monkeypatch, _stubs):
    """Se aplicó un cheque después de la edición. Reversar pisaría ese cobro
    sin avisar: hay que negarse y decir por qué."""
    q, fake = _correr(monkeypatch, _mov(100.0, 500.0), _fact(1000.0, 800.0, "A"))
    with pytest.raises(ValueError, match="cambió después"):
        q.reversar_abono_manual(9001)
    assert not [u for u in fake.updates if "update scintela.factura" in u[0].lower()], (
        "no puede haber escrito nada"
    )


def test_no_reversa_si_el_abono_viejo_ya_no_cabe_en_el_importe(monkeypatch, _stubs):
    """El importe bajó (toggle de pronto pago 5%) y el abono anterior ya no
    entra. Forzarlo dejaría saldo negativo."""
    q, fake = _correr(monkeypatch, _mov(950.0, 500.0), _fact(900.0, 500.0, "A"))
    with pytest.raises(ValueError, match="mayor que el"):
        q.reversar_abono_manual(9001)
    assert not [u for u in fake.updates if "update scintela.factura" in u[0].lower()]


def test_no_reversa_dos_veces(monkeypatch, _stubs):
    q, _ = _correr(monkeypatch, _mov(estado="reversado"), _fact())
    with pytest.raises(ValueError, match="reversado"):
        q.reversar_abono_manual(9001)


def test_rechaza_un_mov_de_otro_tipo(monkeypatch, _stubs):
    """Que el id venga de la URL no significa que sea un abono manual."""
    q, _ = _correr(monkeypatch, _mov(tipo="cheque_aplicado_a_factura"), _fact())
    with pytest.raises(ValueError, match="no un abono manual"):
        q.reversar_abono_manual(9001)


def test_sin_abono_prev_en_la_metadata_manda_a_editar_a_mano(monkeypatch, _stubs):
    """Los movs viejos no guardaron el valor anterior: no se puede inventar."""
    q, _ = _correr(monkeypatch, _mov(meta=False), _fact())
    with pytest.raises(ValueError, match="metadata"):
        q.reversar_abono_manual(9001)


def test_deja_el_mov_original_linkeado_al_reverso(monkeypatch, _stubs):
    """Sin `id_original` el reverso queda huérfano y el chequeo de mov_doble
    lo marca como link roto — el error que estuvimos limpiando hoy."""
    q, _ = _correr(monkeypatch, _mov(100.0, 500.0), _fact(1000.0, 500.0, "A"))
    q.reversar_abono_manual(9001, usuario="tamara", motivo="cero de más")
    assert _stubs, "no registró el mov_doble del reverso"
    kw = _stubs[-1]
    assert kw["tipo"] == "reverso_factura_abono_manual"
    assert kw["id_original"] == 9001
    assert kw["importe"] == -400.0, "el reverso mueve el abono al revés"
    assert "cero de más" in kw["concepto"]


def test_no_toca_una_factura_anulada(monkeypatch, _stubs):
    q, fake = _correr(monkeypatch, _mov(100.0, 500.0), _fact(1000.0, 500.0, "X"))
    with pytest.raises(ValueError, match="anulada"):
        q.reversar_abono_manual(9001)
    assert not [u for u in fake.updates if "update scintela.factura" in u[0].lower()]
