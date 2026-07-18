"""Tests para modules.compras.queries.editar() + crear() extendido.

Verifica:
  - editar concepto/comprobante/observacion siempre permitido.
  - editar importe/fechad: bloqueado si compra pagada.
  - editar importe/fechad propaga a posdat hermana si no pagada.
  - crear con pagada=True+cuenta=caja inserta caja.
  - crear con pagada=True+cuenta=pichincha inserta tx_bancarias DOC=CH.
  - crear con es_anticipo_dolares=True inserta dolares (si prov.tipo HIL/QUI).
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _CompraDB:
    def __init__(self, *, compra: dict | None = None,
                 proveedor: dict | None = None):
        self.compra = compra
        self.proveedor = proveedor or {"id_proveedor": 1, "tipo_prov": "C"}
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []
        self.next_compra_id = 100
        self.next_tx_id = 200
        self.next_caja_id = 300

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.compra where id_compra" in s:
            return dict(self.compra) if self.compra else None
        if "from scintela.proveedor where codigo_prov" in s:
            return dict(self.proveedor) if self.proveedor else None
        # bank/caja saldo previo: devuelve None (banco vacío)
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by fecha desc" in s
        ):
            return None
        if "from scintela.caja" in s and "order by fecha desc" in s:
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
            return {"id_compra": cid, "numero": params[7]}  # 8º param = numero
        if "insert into scintela.transacciones_bancarias" in s:
            tx = self.next_tx_id
            self.next_tx_id += 1
            return {"id_transaccion": tx}
        if "insert into scintela.caja" in s:
            cid = self.next_caja_id
            self.next_caja_id += 1
            return {"id_caja": cid}
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


def _executes_sql(executes):
    return "\n".join(" ".join((s or "").split()).lower() for s, _ in executes)


def _returnings_sql(returnings):
    return "\n".join(" ".join((s or "").split()).lower() for s, _ in returnings)


# --- editar() ----------------------------------------------------------


def test_editar_concepto_y_observacion_libre(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(compra={
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 500, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "viejo",
        "comprobante": "ABC", "stat": None,
        "id_transaccion": None,
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, concepto="nuevo concepto",
                          observacion="aclaracion", usuario="tmt")
    assert res["pagada"] is False
    sqls = _executes_sql(fake.executes)
    assert "update scintela.compra" in sqls


def test_editar_importe_compra_no_pagada_propaga_a_posdat(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(compra={
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 500, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "x",
        "comprobante": "ABC", "stat": None,
        "id_transaccion": None,
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, importe=750, usuario="tmt")
    assert res["importe_nuevo"] == 750.0
    sqls = _executes_sql(fake.executes)
    assert "update scintela.compra" in sqls
    assert "update scintela.posdat" in sqls


def test_editar_importe_compra_pagada_falla(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(compra={
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 500, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "x",
        "comprobante": "ABC", "stat": None,
        "id_transaccion": 999,  # pagada
    })
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="ya pagada"):
        queries.editar(1, importe=750, usuario="tmt")


def test_editar_compra_anulada_falla(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(compra={
        "id_compra": 1, "fecha": date(2026, 4, 30),
        "codigo_prov": "INT", "numero": 100,
        "importe": 500, "fechad": date(2026, 5, 30),
        "tipo": "Q", "concepto": "x",
        "comprobante": None, "stat": "Y",
        "id_transaccion": None,
    })
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="anulada"):
        queries.editar(1, concepto="nuevo", usuario="tmt")


# --- crear() extendido ---------------------------------------------------


def test_crear_pagada_caja_inserta_movimiento_caja(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB()
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="INT",
        importe=200, tipo="C",
        pagada=True, cuenta="caja",
        concepto="aceite",
        usuario="tmt",
    )
    rets = _returnings_sql(fake.execute_returnings)
    assert "insert into scintela.compra" in rets
    assert "insert into scintela.caja" in rets


def test_crear_pagada_pichincha_inserta_tx_bancarias_ch(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB()
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="INT",
        importe=500, tipo="C",
        pagada=True, cuenta="pichincha",
        usuario="tmt",
    )
    rets = _returnings_sql(fake.execute_returnings)
    assert "insert into scintela.transacciones_bancarias" in rets


def test_crear_no_pagada_inserta_posdat(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB()
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="INT",
        importe=300, tipo="Q",
        pagada=False, usuario="tmt",
    )
    sqls = _executes_sql(fake.executes)
    assert "insert into scintela.posdat" in sqls


def test_crear_anticipo_dolares_inserta_dolares(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(proveedor={"id_proveedor": 5, "tipo_prov": "HIL"})
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="HIL",
        importe=1000, tipo="A",
        es_anticipo_dolares=True,
        usuario="tmt",
    )
    sqls = _executes_sql(fake.executes)
    assert "insert into scintela.dolares" in sqls


def test_crear_anticipo_dolares_proveedor_no_hil_no_inserta(monkeypatch):
    """Si el proveedor NO es HIL/QUI, el flag es no-op."""
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(proveedor={"id_proveedor": 5, "tipo_prov": "C"})
    fake.apply_to(monkeypatch, db_mod)

    queries.crear(
        fecha=date(2026, 4, 30),
        codigo_prov="ABC",
        importe=1000, tipo="A",
        es_anticipo_dolares=True,
        usuario="tmt",
    )
    sqls = _executes_sql(fake.executes)
    assert "insert into scintela.dolares" not in sqls


def test_crear_compra_negativa_op_genera_posdat_negativa(monkeypatch):
    """Over-price (OP): una compra NEGATIVA no pagada crea su posdat banc=0
    con importe negativo → cuenta como pasivo negativo (imita COMPRAS.DBF
    BANC#9 del dBase). TMT 2026-06-16.

    Antes esto fallaba por dos guards: (1) el chequeo 'pago_parcial excede
    importe' (0 > -14535) y (2) posdat sólo se creaba si saldo > 0.
    """
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB(proveedor={"id_proveedor": 9, "tipo_prov": "C"})
    fake.apply_to(monkeypatch, db_mod)

    # No debe levantar ValueError ("excede") por ser negativa.
    queries.crear(
        fecha=date(2026, 1, 13),
        codigo_prov="OP",
        importe=-14535.0,
        concepto="MH 56",
        pagada=False,
        usuario="tmt",
    )

    # La posdat se inserta vía execute_returning con importe negativo.
    posdat_ins = [
        params for sql, params in fake.execute_returnings
        if "insert into scintela.posdat" in " ".join((sql or "").split()).lower()
    ]
    assert posdat_ins, "no se creó posdat para la compra negativa OP"
    importe_posdat = posdat_ins[0][4]  # 5º param = importe (saldo_posdat)
    assert importe_posdat < 0, f"posdat debería ser negativa, fue {importe_posdat}"
    assert abs(importe_posdat + 14535.0) < 0.01


def test_crear_compra_negativa_sin_pago_parcial_no_falla(monkeypatch):
    """El guard 'pago_parcial excede importe' NO debe dispararse cuando no
    hay pago parcial real, aunque el importe sea negativo. TMT 2026-06-16."""
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB()
    fake.apply_to(monkeypatch, db_mod)

    # No debe levantar.
    queries.crear(
        fecha=date(2026, 1, 13),
        codigo_prov="OP",
        importe=-7029.0,
        concepto="MH 58",
        usuario="tmt",
    )
    rets = _returnings_sql(fake.execute_returnings)
    assert "insert into scintela.compra" in rets


def test_crear_pagada_cuenta_invalida_falla(monkeypatch):
    import db as db_mod
    from modules.compras import queries

    fake = _CompraDB()
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="(?i)cuenta"):
        queries.crear(
            fecha=date(2026, 4, 30),
            codigo_prov="INT",
            importe=200, tipo="C",
            pagada=True, cuenta="bitcoin",  # inválida
            usuario="tmt",
        )


# ─── edición de TIPO (TMT 2026-07-17: reclasificar NC/QI de Q a C) ───────
def test_editar_tipo_invalido_raise(monkeypatch):
    from modules.compras import queries as q
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: {
        "id_compra": 1, "fecha": None, "codigo_prov": "NC", "numero": 9,
        "importe": 100.0, "fechad": None, "tipo": "Q", "concepto": "x",
        "comprobante": None, "stat": None, "id_transaccion": None,
    })
    with pytest.raises(ValueError, match="Tipo inválido"):
        q.editar(1, tipo="Z")


def test_editar_cambia_tipo_a_c(monkeypatch):
    """Cambiar tipo Q→C actualiza la compra; es clasificación (no toca monto)."""
    from modules.compras import queries as q

    fetches = iter([
        {"id_compra": 1, "fecha": None, "codigo_prov": "NC", "numero": 9,
         "importe": 100.0, "fechad": None, "tipo": "Q", "concepto": "x",
         "comprobante": None, "stat": None, "id_transaccion": None},
        None,  # mov_doble parcial
    ])
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: next(fetches, None))
    ejecutados = []

    class _Tx:
        def __enter__(self):
            return object()
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(q.db, "tx", lambda: _Tx())
    monkeypatch.setattr(
        q.db, "execute",
        lambda sql, params=(), conn=None: ejecutados.append((sql, params)),
    )
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None)
    q.editar(1, tipo="c", usuario="test")
    upd = next(s for s, p in ejecutados if "UPDATE scintela.compra" in s)
    par = next(p for s, p in ejecutados if "UPDATE scintela.compra" in s)
    assert "tipo=%s" in upd
    assert "C" in par
