"""Borrar la fila sobrante de un código de cliente DUPLICADO.

TMT 2026-08-03, dueña, mirando el estado de cuenta: le aparecían dos `GUF`
—"ASOTEXMANA NUBE FAJARDO BORJA" y "GUADALUPE FIALLOS", RUC distintos— con
el MISMO saldo de 15.104,31, porque la pantalla agrupa por código.
Pidió: *"el otro borralo por ahora o dejalo en pausa o en xxx"*.

No se podía. `eliminar_por_id` borra por PK (bien), pero su guarda de FKs
mira por CÓDIGO: GUF tiene 55 facturas y 8 cheques, así que rechazaba las
DOS filas, incluida la sobrante. Y ninguna otra pantalla sirve — `editar`
y `stop` resuelven por `codigo_cli`, y con el código repetido no hay forma
de señalar una fila concreta.

La regla correcta: los movimientos apuntan al CÓDIGO, no al `id_cliente`.
Mientras quede otra fila con ese código, borrar la sobrante no deja nada
huérfano. La guarda sólo debe morder cuando es la ÚLTIMA fila del código.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.clientes import queries as q  # noqa: E402


class _FakeDB:
    """Stub de db: responde según el SQL que le llega."""

    def __init__(self, *, codigo="GUF", otras=0, facturas=0, cheques=0):
        self.codigo, self.otras = codigo, otras
        self.facturas, self.cheques = facturas, cheques
        self.borrados: list[int] = []

    def fetch_one(self, sql, params=None, **kw):
        s = " ".join(sql.split())
        if "SELECT codigo_cli FROM scintela.cliente" in s:
            return {"codigo_cli": self.codigo}
        if "FROM scintela.cliente" in s and "id_cliente <> " in s:
            return {"n": self.otras}
        if "FROM scintela.factura" in s:
            return {"n": self.facturas}
        if "FROM scintela.cheque" in s:
            return {"n": self.cheques}
        raise AssertionError(f"consulta inesperada: {s}")

    def execute(self, sql, params=None, **kw):
        assert "DELETE FROM scintela.cliente" in sql
        self.borrados.append(params[0])
        return 1


@pytest.fixture()
def fake(monkeypatch):
    def _mk(**kw):
        f = _FakeDB(**kw)
        monkeypatch.setattr(q, "db", f)
        return f

    return _mk


def test_borra_la_fila_sobrante_aunque_el_codigo_tenga_movimientos(fake):
    """El caso GUF: 55 facturas + 8 cheques, pero queda la otra fila."""
    f = fake(otras=1, facturas=55, cheques=8)
    assert q.eliminar_por_id(31716) == 1
    assert f.borrados == [31716]


def test_sigue_protegiendo_la_ULTIMA_fila_con_facturas(fake):
    """Sin otra fila que sostenga el código, borrar deja facturas huérfanas."""
    f = fake(otras=0, facturas=55, cheques=0)
    with pytest.raises(ValueError, match="55 facturas"):
        q.eliminar_por_id(30302)
    assert f.borrados == []


def test_sigue_protegiendo_la_ULTIMA_fila_con_cheques(fake):
    f = fake(otras=0, facturas=0, cheques=8)
    with pytest.raises(ValueError, match="8 cheques"):
        q.eliminar_por_id(30302)
    assert f.borrados == []


def test_ultima_fila_sin_movimientos_se_borra(fake):
    """Comportamiento de siempre: cliente limpio, se va."""
    f = fake(otras=0, facturas=0, cheques=0)
    assert q.eliminar_por_id(999) == 1
    assert f.borrados == [999]


def test_fila_sin_codigo_no_consulta_movimientos(fake):
    """Legacy sin código: no puede tener facturas colgando (TMT 2026-05-20)."""
    f = fake(codigo="", otras=0, facturas=99, cheques=99)
    assert q.eliminar_por_id(7) == 1
    assert f.borrados == [7]


def test_el_conteo_de_otras_filas_excluye_la_que_se_borra(fake, monkeypatch):
    """Si contara la propia fila, `otras` daría 1 siempre y la guarda moriría."""
    visto = {}
    f = fake(otras=1, facturas=55, cheques=0)
    original = f.fetch_one

    def espia(sql, params=None, **kw):
        if "id_cliente <> " in " ".join(sql.split()):
            visto["params"] = params
        return original(sql, params, **kw)

    monkeypatch.setattr(f, "fetch_one", espia)
    q.eliminar_por_id(31716)
    assert visto["params"] == ("GUF", 31716)
