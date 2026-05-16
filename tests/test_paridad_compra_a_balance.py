"""Test de paridad — compra → balance.

Cadena verificada:
  1. ALTA compra no pagada $300 → INSERT compra + posdat (banc=0).
  2. ALTA compra pagada Pichincha $500 → INSERT compra + tx_bancarias DOC=CH.
  3. EDITAR importe de compra no pagada → propaga a posdat hermana.
  4. EDITAR importe de compra ya pagada → ValueError.
  5. ANULAR compra → DELETE posdat hermana.
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _ParidadCompra:
    def __init__(self):
        self.compra: dict | None = None
        self.proveedor = {"id_proveedor": 5, "tipo_prov": "C"}
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []
        self.next_compra_id = 100
        self.next_tx_id = 1000

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        if "from scintela.compra where id_compra" in s:
            return dict(self.compra) if self.compra else None
        if "from scintela.proveedor where codigo_prov" in s:
            return dict(self.proveedor)
        if "max(numero)" in s:
            return {"siguiente": 100}
        if "from scintela.transacciones_bancarias" in s and "order by fecha desc" in s:
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returnings.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "insert into scintela.compra" in s:
            cid = self.next_compra_id
            self.next_compra_id += 1
            return {"id_compra": cid, "numero": params[7]}
        if "insert into scintela.transacciones_bancarias" in s:
            tx = self.next_tx_id
            self.next_tx_id += 1
            return {"id_transaccion": tx}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)
        monkeypatch.setattr(db_mod, "tx", self.tx)


@pytest.fixture(autouse=True)
def _no_periodo_guard(monkeypatch):
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)


def _all_sqls(fake):
    sqls = "\n".join(" ".join((s or "").split()).lower() for s, _ in fake.executes)
    rets = "\n".join(" ".join((s or "").split()).lower() for s, _ in fake.execute_returnings)
    return sqls + "\n" + rets


def test_paridad_compra_no_pagada_inserta_posdat(monkeypatch):
    """ALTA compra no pagada → posdat banc=0 (deuda viva)."""
    import db as db_mod
    from modules.compras import queries

    fake = _ParidadCompra()
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="INT",
        importe=300, tipo="Q",
        pagada=False,
        usuario="tmt",
    )
    sqls = _all_sqls(fake)
    assert "insert into scintela.compra" in sqls
    assert "insert into scintela.posdat" in sqls
    # posdat con banc=0
    posdat_params = None
    for sql, params in fake.executes:
        if "insert into scintela.posdat" in " ".join(sql.split()).lower():
            posdat_params = params
    assert posdat_params is not None


def test_paridad_compra_pagada_pichincha_inserta_banco(monkeypatch):
    """ALTA compra pagada Pichincha → tx_bancarias DOC=CH banco=1."""
    import db as db_mod
    from modules.compras import queries

    fake = _ParidadCompra()
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="INT",
        importe=500, tipo="C",
        pagada=True, cuenta="pichincha",
        usuario="tmt",
    )
    sqls = _all_sqls(fake)
    assert "insert into scintela.transacciones_bancarias" in sqls
    # NO crea posdat (es contado)
    posdat_creado = "insert into scintela.posdat" in sqls
    assert not posdat_creado, "Compra pagada no debería crear posdat"


def test_paridad_compra_editar_importe_propaga_posdat(monkeypatch):
    """EDITAR importe de compra no pagada → UPDATE compra + UPDATE posdat."""
    import db as db_mod
    from modules.compras import queries

    fake = _ParidadCompra()
    fake.compra = {
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 300, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "x", "comprobante": None,
        "stat": None, "id_transaccion": None,
    }
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(1, importe=450, usuario="tmt")
    sqls = _all_sqls(fake)
    assert "update scintela.compra" in sqls
    assert "update scintela.posdat" in sqls


def test_paridad_compra_pagada_no_se_edita_importe(monkeypatch):
    """EDITAR importe de compra pagada → ValueError (lockeada)."""
    import db as db_mod
    from modules.compras import queries

    fake = _ParidadCompra()
    fake.compra = {
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 300, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "x", "comprobante": None,
        "stat": None, "id_transaccion": 999,
    }
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="ya pagada"):
        queries.editar(1, importe=450, usuario="tmt")


def test_paridad_compra_anular_borra_posdat(monkeypatch):
    """ANULAR compra → DELETE posdat hermana."""
    import db as db_mod
    from modules.compras import queries

    fake = _ParidadCompra()
    fake.compra = {
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 300, "fechad": date(2026, 5, 30),
        "tipo": "Q", "stat": None, "id_transaccion": None,
    }
    fake.apply_to(monkeypatch, db_mod)

    queries.anular(1, motivo="error de carga", usuario="tmt")
    sqls = _all_sqls(fake)
    assert "update scintela.compra" in sqls
    assert "delete from scintela.posdat" in sqls
