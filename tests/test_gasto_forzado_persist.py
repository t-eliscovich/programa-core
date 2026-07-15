"""TMT 2026-07-15 (dueña: "no se pueden crear gastos forzados o no se ven").

Regresión: `gasto_forzado_crear/actualizar/eliminar` usaban db.fetch_one() para
sus INSERT/UPDATE/DELETE ... RETURNING. fetch_one() NO commitea, así que el
write se rollbackeaba al devolver la conexión al pool: el endpoint respondía 201
con el id (venía del RETURNING dentro de la tx) pero la fila nunca persistía, y
el listar salía siempre vacío ("no se ven"). El fix los pasa a
db.execute_returning() (commitea cuando es dueño de la conexión).

Estos tests fallan si alguien vuelve a usar fetch_one() para el write: el stub
de fetch_one devuelve None para cualquier RETURNING, así que crear/eliminar
"perderían" la fila.
"""
import pytest


class _DBStub:
    def __init__(self):
        self.execute_returning_calls: list = []
        self.fetch_one_calls: list = []
        self.fetch_one_responses: list = []
        self.execute_returning_responses: list = []

    def fetch_one(self, sql, params=None, conn=None):
        self.fetch_one_calls.append((sql, tuple(params or ())))
        return self.fetch_one_responses.pop(0) if self.fetch_one_responses else None

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        return (
            self.execute_returning_responses.pop(0)
            if self.execute_returning_responses
            else None
        )


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    return s


def test_crear_usa_execute_returning_commit(stub):
    """Crear debe ir por execute_returning (commitea), NO por fetch_one."""
    from modules.informes import queries as q
    stub.execute_returning_responses = [
        {"id_gasto_forzado": 7, "fecha": None, "importe": 500.0,
         "concepto": "hilado", "prov": "AC", "version": 1}
    ]
    item = q.gasto_forzado_crear(fecha="2026-08-01", importe=500.0,
                                 concepto="hilado", prov="AC", usuario="tamara")
    assert item["id"] == 7
    assert item["prov"] == "AC"
    assert len(stub.execute_returning_calls) == 1, \
        "el INSERT debe correr por execute_returning (commitea)"
    assert "INSERT INTO scintela.gasto_forzado" in stub.execute_returning_calls[0][0]
    assert stub.fetch_one_calls == [], \
        "el INSERT NO debe ir por fetch_one (no commitea → se rollbackea)"


def test_eliminar_usa_execute_returning_commit(stub):
    """Eliminar debe ir por execute_returning (commitea el DELETE)."""
    from modules.informes import queries as q
    stub.execute_returning_responses = [{"id_gasto_forzado": 7}]
    assert q.gasto_forzado_eliminar(7) is True
    assert len(stub.execute_returning_calls) == 1
    assert "DELETE FROM scintela.gasto_forzado" in stub.execute_returning_calls[0][0]
    assert stub.fetch_one_calls == [], "el DELETE no debe ir por fetch_one"


def test_actualizar_usa_execute_returning_para_el_update(stub):
    """Actualizar lee por fetch_one (SELECT, ok) pero el UPDATE va por
    execute_returning (commitea)."""
    from modules.informes import queries as q
    # 1) SELECT del actual (read) → fetch_one
    stub.fetch_one_responses = [
        {"id_gasto_forzado": 7, "fecha": None, "importe": 500.0,
         "concepto": "hilado", "prov": "AC", "version": 1}
    ]
    # 2) UPDATE ... RETURNING → execute_returning
    stub.execute_returning_responses = [
        {"id_gasto_forzado": 7, "fecha": None, "importe": 900.0,
         "concepto": "hilado", "prov": "AC", "version": 2}
    ]
    r = q.gasto_forzado_actualizar(id_gasto_forzado=7, expected_version=1,
                                   importe=900.0, usuario="tamara")
    assert r["ok"] is True
    assert r["updated"]["version"] == 2
    assert len(stub.execute_returning_calls) == 1, \
        "el UPDATE debe correr por execute_returning (commitea)"
    assert "UPDATE scintela.gasto_forzado" in stub.execute_returning_calls[0][0]
