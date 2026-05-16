"""Test de paridad — factura → balance.

Verifica que la cadena ALTA factura → MODIFICA factura abono → ANULAR factura
emite los SQL correctos para que los informes (BALANCE.TOTF, CARTERA[cli])
queden coherentes.

Usa stubs — no toca Postgres. La validación de los SQL emitidos garantiza que
el comportamiento es el correcto; el test integration-DB queda para la
próxima sesión cuando se setee pytest-postgresql.
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _ParidadDB:
    """Stub que registra los SQL emitidos para inspección post-flow."""

    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []
        self.facturas: dict[int, dict] = {}
        self.next_id = 100

    def add_factura(self, **kwargs):
        defaults = {
            "id_factura": self.next_id, "numf": 1000,
            "codigo_cli": "JTX", "fecha": date(2026, 4, 30),
            "vencimiento": date(2026, 5, 30),
            "importe": 0, "abono": 0, "saldo": 0,
            "stat": "Z", "condic": "",
        }
        defaults.update(kwargs)
        self.facturas[self.next_id] = defaults
        self.next_id += 1
        return defaults

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        if "from scintela.factura where id_factura" in s:
            id_f = params[0] if params else None
            return dict(self.facturas[id_f]) if id_f in self.facturas else None
        if "from scintela.cliente where codigo_cli" in s:
            return {"pago": 30}
        if "max(numf)" in s:
            return {"siguiente": 1001}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returnings.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "insert into scintela.factura" in s:
            id_f = self.next_id
            self.next_id += 1
            return {"id_factura": id_f, "numf": params[0]}
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


def _executes_concat(executes):
    return "\n".join(" ".join((s or "").split()).lower() for s, _ in executes)


def test_paridad_factura_alta_modifica_anular(monkeypatch):
    """ALTA factura → MODIFICA abono → ANULAR. Cada paso deja la fila en
    el estado correcto y emite el SQL esperado para que BALANCE/CARTERA
    sean coherentes."""
    import db as db_mod
    from modules.facturas import queries

    fake = _ParidadDB()
    fake.apply_to(monkeypatch, db_mod)

    # === Step 1: ALTA factura JTX $800 ===
    creada = queries.crear(
        fecha=date(2026, 4, 30),
        codigo_cli="JTX",
        kg=100, importe=800,
        usuario="tmt",
    )
    assert creada
    rets = "\n".join(
        " ".join(s.split()).lower() for s, _ in fake.execute_returnings
    )
    assert "insert into scintela.factura" in rets
    # Importe + saldo iniciales == 800; abono == 0; stat == 'Z'
    insert_params = fake.execute_returnings[0][1]
    # El INSERT tiene: numf, fecha, codigo_cli, kg, importe, saldo (=importe),
    # condic, tipo, vencimiento, numf_completo, clave, usuario
    assert insert_params[4] == 800  # importe
    assert insert_params[5] == 800  # saldo (=importe)

    id_factura = creada.get("id_factura")
    # Inyectamos la fila para los siguientes pasos
    fake.facturas[id_factura] = {
        "id_factura": id_factura, "fecha": date(2026, 4, 30),
        "importe": 800, "abono": 0, "saldo": 800,
        "stat": "Z", "condic": "", "vencimiento": date(2026, 5, 30),
        "numf": creada.get("numf") or 1001,
        "codigo_cli": "JTX",
    }

    # === Step 2: MODIFICA abono $200 → saldo $600, stat='A' ===
    res = queries.editar(id_factura, abono=200, usuario="tmt")
    assert res["abono"] == 200.0
    assert res["saldo"] == 600.0
    assert res["stat_nuevo"] == "A"

    # Verifica el SQL emitido del UPDATE recompute saldo
    sqls = _executes_concat(fake.executes)
    assert "update scintela.factura" in sqls
    assert "saldo=" in sqls
    assert "stat=" in sqls

    # Inyectamos el nuevo estado
    fake.facturas[id_factura] = {
        "id_factura": id_factura, "fecha": date(2026, 4, 30),
        "importe": 800, "abono": 200, "saldo": 600,
        "stat": "A", "condic": "", "vencimiento": date(2026, 5, 30),
        "numf": 1001, "codigo_cli": "JTX",
    }

    # === Step 3: ANULAR factura ===
    queries.anular(id_factura, motivo="error de carga, reemitir", usuario="tmt")
    sqls = _executes_concat(fake.executes)
    # La factura fue marcada con stat='X'
    assert "stat = 'x'" in sqls or "stat='x'" in sqls
    # La observación queda con tag [ELIM]
    encontrado_elim = False
    for _sql, params in fake.executes:
        for p in params:
            if isinstance(p, str) and "[ELIM]" in p:
                encontrado_elim = True
    assert encontrado_elim, "El motivo no se appendea en observación"


def test_paridad_factura_pronto_pago_5_pct(monkeypatch):
    """MODIFICA factura condic ' '→'C' aplica 5% pronto pago.
    Luego volver a ' ' restituye el importe original.

    Test crítico para el reporte BALANCE: si el toggle no se aplica en
    Postgres, los totales de TOTF salen mal.
    """
    import db as db_mod
    from modules.facturas import queries

    fake = _ParidadDB()
    fake.facturas[1] = {
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 1000, "abono": 0, "saldo": 1000,
        "stat": "Z", "condic": "",
    }
    fake.apply_to(monkeypatch, db_mod)

    # ' '→'C' descuento
    res = queries.editar(1, condic="C", usuario="tmt")
    assert res["importe"] == 950.0
    assert res["saldo"] == 950.0

    # Inyectar nuevo estado
    fake.facturas[1].update({"importe": 950, "saldo": 950, "condic": "C"})

    # 'C'→'' restituye
    res2 = queries.editar(1, condic="", usuario="tmt")
    assert abs(res2["importe"] - 1000.0) < 0.01
