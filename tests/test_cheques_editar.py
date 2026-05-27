"""Tests para modules.cheques.queries — editar(), transicionar_stat(),
anular_por_error_de_carga(). Decisión 2026-04-30 (addendum batch 22).

No tocan Postgres — monkeypatchean db.* y bank_helpers/caja_helpers para
verificar que los side-effects se invocan con los args correctos.
"""

from __future__ import annotations

import contextlib
from datetime import date, timedelta
from typing import Any

import pytest


class _RecorderDB:
    """Stub flexible — devuelve filas pre-cargadas y registra todas las queries."""

    def __init__(
        self,
        *,
        cheque: dict | None = None,
        aplic: list[dict] | None = None,
        facturas: dict[int, dict] | None = None,
    ):
        self.cheque = cheque
        self.aplic = list(aplic or [])
        self.facturas = dict(facturas or {})
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []
        self.next_tx_id = 100
        self.next_caja_id = 200

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.cheque) if self.cheque else None
        if "from scintela.factura where id_factura" in s:
            id_f = params[0] if params else None
            return dict(self.facturas[id_f]) if id_f in self.facturas else None
        # bank_helpers _saldo_previo: no_banco=X & order by fecha desc
        if "from scintela.transacciones_bancarias" in s and "order by fecha desc, id_transaccion desc" in s:
            return None  # banco vacío en estos tests
        if "from scintela.transacciones_bancarias" in s and "where id_transaccion =" in s:
            return None
        # caja _saldo_previo
        if "from scintela.caja" in s and "order by fecha desc, id_caja desc" in s:
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.chequesxfact where id_cheque" in s:
            return [dict(a) for a in self.aplic]
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returnings.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
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


def _executes_str(executes: list[tuple[str, tuple]]) -> str:
    return "\n".join(" ".join((s or "").split()).lower() for s, _ in executes)


# --- editar() -------------------------------------------------------------


def test_editar_concepto_y_observacion(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "fechad": date(2026, 5, 1),
            "concepto": "viejo",
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, concepto="nuevo concepto", observacion="ajuste", usuario="tmt")
    assert res["id_cheque"] == 1
    assert res["fechad_shifted_lunes"] is False
    sqls = _executes_str(fake.executes)
    assert "update scintela.cheque" in sqls
    # TMT 2026-05-26: scintela.cheque no tiene columna `concepto`. Si el
    # form manda concepto, se stashea en observacion con prefix [C].
    body = str(fake.executes).lower()
    assert "[c] nuevo concepto" in body
    assert "[e] ajuste" in body


def test_editar_fechad_domingo_shifta_a_lunes(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "fechad": date(2026, 5, 1),
            "concepto": "x",
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    domingo = date(2026, 5, 3)  # mayo 3 2026 = domingo
    assert domingo.weekday() == 6
    res = queries.editar(1, fechad=domingo, usuario="tmt")
    assert res["fechad_shifted_lunes"] is True
    assert res["fechad_nueva"] == domingo + timedelta(days=1)


def test_editar_depositado_permite_fechad(monkeypatch):
    """TMT 2026-05-27 dueña: 'dejame editar deposito de cheque'.
    Antes esto levantaba ValueError; ahora se permite (la transición de
    stat sigue requiriendo flujo formal — esto solo edita la FECHA del
    depósito ya hecho, para cuadrar con extracto banco)."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "fechad": date(2026, 5, 1),
            "concepto": "x",
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, fechad=date(2026, 6, 1), usuario="tmt")
    assert res["fechad_nueva"] == date(2026, 6, 1)
    body = str(fake.executes).lower()
    assert "fechad=%s" in body
    assert "usuario_modifica=%s" in body


def test_editar_importe_valido(monkeypatch):
    """TMT 2026-05-27 dueña: 'dejame editar valor de cheque!!'."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1, "no_cheque": "001", "stat": "Z",
            "fechad": date(2026, 5, 1), "importe": 100.00,
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(1, importe=250.50, usuario="tmt")
    body = str(fake.executes).lower()
    assert "importe=%s" in body


def test_editar_importe_invalido(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(cheque={"id_cheque": 1, "no_cheque": "001", "stat": "Z", "fechad": None})
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match=">"):
        queries.editar(1, importe=0, usuario="tmt")
    with pytest.raises(ValueError, match="máximo|maximo"):
        queries.editar(1, importe=10_000_000, usuario="tmt")


def test_editar_no_cheque_valido(monkeypatch):
    """TMT 2026-05-27 dueña: 'tambien se tiene que ver el numero de
    documento y poder editar este' — antes requería anular+reemitir."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1, "no_cheque": "001", "stat": "Z",
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(1, no_cheque="00999", usuario="tmt")
    body_sqls = " ".join(s for s, _ in fake.executes).lower()
    assert "no_cheque=%s" in body_sqls
    all_params = [p for _, params in fake.executes for p in params]
    assert "00999" in all_params


def test_editar_no_cheque_vacio_falla(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(cheque={"id_cheque": 1, "no_cheque": "001", "stat": "Z", "fechad": None})
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="vacío|vacio"):
        queries.editar(1, no_cheque="   ", usuario="tmt")


def test_editar_no_cheque_demasiado_largo_falla(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(cheque={"id_cheque": 1, "no_cheque": "001", "stat": "Z", "fechad": None})
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="10 caracteres"):
        queries.editar(1, no_cheque="12345678901", usuario="tmt")


def test_editar_no_cheque_igual_no_genera_update(monkeypatch):
    """Si el no_cheque mandado es == al actual, no se incluye en el UPDATE
    de no_cheque (evita escritura inútil) pero sí actualiza usuario_modifica."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(cheque={"id_cheque": 1, "no_cheque": "001", "stat": "Z", "fechad": None})
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(1, no_cheque="001", usuario="tmt")
    body_sqls = " ".join(s for s, _ in fake.executes).lower()
    assert "no_cheque=%s" not in body_sqls
    assert "usuario_modifica=%s" in body_sqls


def test_editar_stat_terminal_falla(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "X",
            "fechad": date(2026, 5, 1),
            "concepto": "x",
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="terminal"):
        queries.editar(1, concepto="nuevo", usuario="tmt")


# --- transicionar_stat() -------------------------------------------------


def test_transicionar_z_a_b_inserta_movimiento_bancario(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
            "banco": None,
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.transicionar_stat(1, stat_destino="B", no_banco=1, usuario="tmt")
    assert res["stat_nuevo"] == "B"
    assert res["side_effect_id"] is not None  # tx_bancarias creada
    sqls = _executes_str(fake.executes)
    assert "update scintela.cheque" in sqls
    # Verifica que se creó la fila bancaria
    rets = "\n".join(" ".join(s.split()).lower() for s, _ in fake.execute_returnings)
    assert "insert into scintela.transacciones_bancarias" in rets


def test_transicionar_z_a_c_inserta_caja(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
            "banco": None,
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.transicionar_stat(1, stat_destino="C", usuario="tmt")
    assert res["stat_nuevo"] == "C"
    assert res["side_effect_id"] is not None  # caja creada
    rets = "\n".join(" ".join(s.split()).lower() for s, _ in fake.execute_returnings)
    assert "insert into scintela.caja" in rets


def test_transicionar_a_9_marca_cliente_stop_y_crea_posdat(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": 1,
            "fechad": date(2026, 5, 1),
            "banco": "Pichincha",
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.transicionar_stat(1, stat_destino="9", motivo="rebotado", usuario="tmt")
    assert res["stat_nuevo"] == "9"
    sqls = _executes_str(fake.executes)
    assert "insert into scintela.posdat" in sqls
    assert "update scintela.cliente" in sqls and "stop='s'" in sqls


def test_transicion_no_valida_falla(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": 1,
            "fechad": date(2026, 5, 1),
            "banco": None,
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    # B no se puede ir a P (sólo a 9 o X)
    with pytest.raises(ValueError, match="no permitida"):
        queries.transicionar_stat(1, stat_destino="P", usuario="tmt")


# --- anular_por_error_de_carga() -----------------------------------------


def test_anular_error_carga_motivo_corto_ahora_permitido(monkeypatch):
    """TMT 2026-05-21 dueña: el motivo ya no tiene minlen 10.

    Antes este test verificaba que motivo<10 chars levantara. Ahora
    cualquier motivo (incluso vacío) se acepta — la dueña no quiere
    fricción al anular un cheque cargado por error.
    """
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    # No debe levantar — motivo corto ya no es un blocker.
    queries.anular_por_error_de_carga(1, motivo="corto", usuario="tmt")


def test_anular_error_carga_z_no_compensa(monkeypatch):
    """Cheque en cartera (Z): NO debería insertar compensación."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.anular_por_error_de_carga(1, motivo="duplicado por error", usuario="tmt")
    assert res["stat_nuevo"] == "X"
    assert res["compensacion"] is None
    rets = "\n".join(" ".join(s.split()).lower() for s, _ in fake.execute_returnings)
    assert "insert into scintela.transacciones_bancarias" not in rets
    assert "insert into scintela.caja" not in rets


def test_anular_error_carga_b_inserta_compensacion_banco(monkeypatch):
    """Cheque depositado (B): inserta ND compensatoria en transacciones_bancarias."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": 1,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.anular_por_error_de_carga(1, motivo="importe mal cargado fue 50 no 500", usuario="tmt")
    assert res["stat_nuevo"] == "X"
    assert res["compensacion"] is not None
    assert res["compensacion"]["tipo"] == "banco"
    rets = "\n".join(" ".join(s.split()).lower() for s, _ in fake.execute_returnings)
    assert "insert into scintela.transacciones_bancarias" in rets


def test_anular_error_carga_c_inserta_salida_caja(monkeypatch):
    """Cheque cobrado en caja (C): inserta TIPO=S compensatoria."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "C",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    res = queries.anular_por_error_de_carga(1, motivo="cliente equivocado fue ABC", usuario="tmt")
    assert res["compensacion"]["tipo"] == "caja"
    rets = "\n".join(" ".join(s.split()).lower() for s, _ in fake.execute_returnings)
    assert "insert into scintela.caja" in rets


def test_anular_error_carga_NO_marca_cliente_stop(monkeypatch):
    """Crítico: anular por error NO marca cliente.stop (no es rebote real)."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": 1,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.anular_por_error_de_carga(1, motivo="error de tipeo en importe", usuario="tmt")
    sqls = _executes_str(fake.executes)
    # Asegurar que NO hay UPDATE cliente SET stop='S'
    assert "update scintela.cliente" not in sqls or "stop='s'" not in sqls


def test_anular_error_carga_aplica_id_reemplazo_en_obs(monkeypatch):
    """Si pasamos id_reemplazo, queda en la observación del cheque anulado."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "Z",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": None,
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.anular_por_error_de_carga(1, motivo="reemplaza por nuevo", id_reemplazo=42, usuario="tmt")
    # La marca con "reemplaza por #42" tiene que estar en alguno de los params
    encontrado = False
    for _sql, params in fake.executes:
        for p in params:
            if isinstance(p, str) and "#42" in p:
                encontrado = True
    assert encontrado, "El id_reemplazo no aparece en la observación"


def test_anular_error_carga_revierte_aplicaciones_a_facturas(monkeypatch):
    """Si el cheque tenía aplicaciones, las revierte (factura.abono -=)."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 1,
            "no_cheque": "001",
            "stat": "B",
            "codigo_cli": "JTX",
            "importe": 500,
            "no_banco": 1,
            "fechad": date(2026, 5, 1),
        },
        aplic=[{"id_chequexfact": 1, "id_fact": 99, "importe": 300}],
        facturas={99: {"id_factura": 99, "importe": 1000, "abono": 300}},
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.anular_por_error_de_carga(1, motivo="todo mal cargado", usuario="tmt")
    # Verifica que se actualizó factura.abono (revertido a 0) y saldo a 1000
    factura_updates = [
        (sql, params)
        for sql, params in fake.executes
        if "update scintela.factura" in " ".join(sql.split()).lower()
    ]
    assert len(factura_updates) >= 1
