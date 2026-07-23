"""Un cheque DEPOSITADO que se marca devuelto (1/2/3) debe descontar su
importe del banco con una nota de débito (ND), sino queda contado doble
(banco + cartera viva). TMT 2026-07-23 (dueña: "la devolución no aparece en
banco"). Ver queries.compensar_deposito_devuelto.
"""

import contextlib
from datetime import date


class _Rec:
    """Stub de db.* para el helper: simula el link a un depósito 'DE' y captura
    las NDs insertadas y los DELETE de link."""

    def __init__(self, *, tiene_link: bool, ya_nd: bool):
        self.tiene_link = tiene_link
        self.ya_nd = ya_nd
        self.executes: list[tuple[str, tuple]] = []
        self.nds: list[dict] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "numreferencia = %s" in s and "'nd'" in s:
            return {"x": 1} if self.ya_nd else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "chequextransaccion cxt" in s and "'de'" in s:
            return [{"id_transaccion": 777, "no_banco": 10}] if self.tiene_link else []
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _apply(monkeypatch, rec):
    import db

    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(db, "execute", rec.execute)

    import bank_helpers

    def _fake_insert(conn, **kw):
        rec.nds.append(kw)
        return {"id_transaccion": 12345, "saldo_nuevo": 0.0}

    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", _fake_insert)


def _tiene_delete_link(executes):
    return any(
        "delete from scintela.chequextransaccion" in " ".join(sql.split()).lower()
        for sql, _ in executes
    )


def test_compensa_genera_nd_y_desagrupa(monkeypatch):
    """Con depósito 'DE' vivo y sin ND previa → inserta ND por el importe y
    borra el link al depósito."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=False)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(),
        id_cheque=100688,
        importe=21508.62,
        codigo_cli="BYG",
        no_cheque="14778",
        fecha=date(2026, 7, 23),
        usuario="tmt",
    )

    assert monto == 21508.62
    assert len(rec.nds) == 1
    nd = rec.nds[0]
    assert nd["documento"] == "ND"
    assert nd["importe"] == 21508.62
    assert nd["no_banco"] == 10
    assert nd["numreferencia"] == 100688
    assert _tiene_delete_link(rec.executes)


def test_compensa_idempotente_si_ya_hay_nd(monkeypatch):
    """Si ya existe una ND para el cheque → no hace nada (evita doble descuento)."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=True)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(), id_cheque=100688, importe=21508.62, codigo_cli="BYG",
        no_cheque="14778", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert monto == 0.0
    assert rec.nds == []
    assert not _tiene_delete_link(rec.executes)


def test_compensa_noop_sin_deposito(monkeypatch):
    """Cheque que nunca se depositó (sin link 'DE' vivo) → no toca el banco."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=False, ya_nd=False)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(), id_cheque=999, importe=500.0, codigo_cli="ZZZ",
        no_cheque="1", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert monto == 0.0
    assert rec.nds == []
    assert not _tiene_delete_link(rec.executes)
